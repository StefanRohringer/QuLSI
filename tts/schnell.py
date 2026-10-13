"""Qwen3-TTS schneller: der Code-Predictor ohne den Umweg über transformers.generate().

Qwen3-TTS erzeugt 12 Audio-Rahmen pro Sekunde. Für jeden Rahmen ruft das Modell (qwen-tts 0.1.1) für die
übrigen 15 Codebücher ein eigenes `code_predictor.generate()` auf. Diese Hülle kostet pro Aufruf mehr als die
eigentliche Rechnung (GenerationConfig kopieren, Logits-Prozessoren aufbauen, Stopp-Kriterien, im Log jedes Mal
„code_predictor_config is None ...“). Hier dieselben 15 Schritte als einfache Schleife mit KV-Cache und derselben
Auswahl (Temperatur, Top-k, Top-p wie in transformers). Abschalten: TTS_SCHNELL=aus.

Auf der Grafikkarte zusätzlich als CUDA-Graph: die 15 Schritte sind je Rahmen immer gleich groß (Batch x 2 Vektoren
rein, 15 Codes raus). Einmal aufgenommen, startet die Karte alle rund 400 kleinen Kernel mit einem Aufruf statt
jeden einzeln aus Python. Das ist bei so kleinen Modellen der größte Zeitfresser. Klappt die Aufnahme nicht
(Treiber, transformers-Version), bleibt es bei der einfachen Schleife. Abschalten: TTS_CUDA_GRAPH=aus.
"""

from __future__ import annotations

import logging
import time
import types

log = logging.getLogger("tts")


def auswaehlen(logits, do_sample: bool, top_k: int | None, top_p: float | None, temperature: float | None,
               generator=None):
    """Nächstes Token je Zeile, in derselben Reihenfolge wie transformers: Temperatur, Top-k, Top-p."""
    import torch

    if not do_sample:
        return logits.argmax(dim=-1)
    werte = logits.float()
    if temperature and temperature != 1.0:
        werte = werte / temperature
    if top_k and 0 < top_k < werte.shape[-1]:
        grenze = torch.topk(werte, top_k, dim=-1).values[..., -1, None]
        werte = werte.masked_fill(werte < grenze, float("-inf"))
    if top_p is not None and top_p < 1.0:
        sortiert, idx = torch.sort(werte, descending=False)
        kumuliert = sortiert.softmax(dim=-1).cumsum(dim=-1)
        weg = kumuliert <= (1 - top_p)
        weg[..., -1:] = False  # mindestens ein Token bleibt
        werte = werte.masked_fill(weg.scatter(-1, idx, weg), float("-inf"))
    return torch.multinomial(werte.softmax(dim=-1), 1, generator=generator).squeeze(-1)


def _generieren(cp, inputs_embeds=None, max_new_tokens: int = 15, do_sample: bool = True, top_p=None, top_k=None,
                temperature=None, **_):
    """Ersatz für code_predictor.generate(): gibt wie dort ein Objekt mit .sequences (Batch x max_new_tokens)."""
    import torch
    from transformers.cache_utils import DynamicCache

    cache = DynamicCache()
    out = cp(inputs_embeds=inputs_embeds, past_key_values=cache, use_cache=True)
    schritt = out.generation_steps
    token = auswaehlen(out.logits[:, -1, :], do_sample, top_k, top_p, temperature)
    folge = [token]
    for _ in range(max_new_tokens - 1):
        out = cp(input_ids=token[:, None], past_key_values=cache, use_cache=True, generation_steps=schritt)
        schritt = out.generation_steps
        token = auswaehlen(out.logits[:, -1, :], do_sample, top_k, top_p, temperature)
        folge.append(token)
    return types.SimpleNamespace(sequences=torch.stack(folge, dim=1))


class Graph:
    """Code-Predictor als CUDA-Graph, je Form und Auswahl-Einstellung einmal aufgenommen."""

    def __init__(self, cp):
        self.cp = cp
        self.graphen: dict = {}
        self.aus = False

    def __call__(self, inputs_embeds=None, max_new_tokens: int = 15, do_sample: bool = True, top_p=None, top_k=None,
                 temperature=None, **_):
        kw = {"max_new_tokens": max_new_tokens, "do_sample": do_sample, "top_p": top_p, "top_k": top_k,
              "temperature": temperature}
        if self.aus or inputs_embeds is None or inputs_embeds.device.type != "cuda":
            return _generieren(self.cp, inputs_embeds=inputs_embeds, **kw)
        schluessel = (tuple(inputs_embeds.shape), inputs_embeds.dtype, max_new_tokens, do_sample, top_p, top_k,
                      temperature)
        g = self.graphen.get(schluessel)
        if g is None:
            try:
                g = self.graphen[schluessel] = self.aufnehmen(inputs_embeds, kw)
                log.info("Qwen3-TTS: Code-Predictor als CUDA-Graph aufgenommen %s", tuple(inputs_embeds.shape))
            except Exception as exc:  # noqa: BLE001  dann eben ohne Graph
                log.warning("Qwen3-TTS: CUDA-Graph geht nicht (%s: %s), bleibe bei der Schleife",
                            type(exc).__name__, str(exc)[:200])
                self.aus = True
                return _generieren(self.cp, inputs_embeds=inputs_embeds, **kw)
        ein, aus, graph = g
        ein.copy_(inputs_embeds)
        graph.replay()
        return types.SimpleNamespace(sequences=aus.clone())

    def aufnehmen(self, inputs_embeds, kw):
        import torch

        ein = inputs_embeds.detach().clone()
        nebenher = torch.cuda.Stream()
        nebenher.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(nebenher):  # Aufwärmen außerhalb der Aufnahme (cuBLAS-Arbeitsspeicher usw.)
            for _ in range(3):
                _generieren(self.cp, inputs_embeds=ein, **kw)
        torch.cuda.current_stream().wait_stream(nebenher)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            aus = _generieren(self.cp, inputs_embeds=ein, **kw).sequences
        graph.replay()  # einmal probeweise, Fehler sollen hier auffallen und nicht im ersten echten Satz
        torch.cuda.synchronize()
        return ein, aus, graph


class Messung:
    """Zeit im Code-Predictor je Satz, damit das Log zeigt, wo die Zeit bleibt."""

    def __init__(self, f):
        self.f = f
        self.rahmen = 0
        self.sekunden = 0.0

    def __call__(self, *args, **kw):
        t = time.perf_counter()
        out = self.f(*args, **kw)
        seq = out.sequences
        if seq.device.type == "cuda":
            import torch
            torch.cuda.synchronize()
        self.rahmen += 1
        self.sekunden += time.perf_counter() - t
        return out

    def abholen(self) -> tuple[int, float]:
        werte = (self.rahmen, self.sekunden)
        self.rahmen, self.sekunden = 0, 0.0
        return werte


def beschleunigen(modell, graph: bool = True) -> bool:
    """modell: Qwen3TTSModel (qwen_tts). Ersetzt generate() des Code-Predictors nur für diese Instanz."""
    try:
        cp = modell.model.talker.code_predictor
    except AttributeError:
        log.warning("Qwen3-TTS: Code-Predictor nicht gefunden, bleibe bei generate()")
        return False
    f = Graph(cp) if graph else types.MethodType(_generieren, cp)
    cp.generate = Messung(f)
    return True


def messung(modell) -> tuple[int, float]:
    """(Rahmen, Sekunden im Code-Predictor) seit dem letzten Abholen; (0, 0) ohne Messung."""
    try:
        return modell.model.talker.code_predictor.generate.abholen()
    except AttributeError:
        return 0, 0.0
