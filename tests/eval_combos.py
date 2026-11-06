"""Combinations of the benchmarked tools, simulated per case from eval_deskew.py's results.

A pipeline takes the real answers of fast tools for a case and only falls back to tidyscan when
they disagree; its outcome is scored with eval_deskew.evaluate(), on the same cases for every pipeline
(all cases with an exact answer, the mirror grid apart), so the numbers are comparable, unlike the
per-tool tables where skew-only tools skip turned pages.

    python3 tests/eval_combos.py WORK EXTRA

WORK is eval_deskew's work directory; EXTRA holds jdeskew-45, cv-hough and leptonica on the turned pages
eval_deskew skips for skew-only tools (their angles are used; their timings overlapped other work,
so for those cases the tool's median time is used instead).

Simplifications, stated in the output: the fast stages' times are added up as if run one after the
other; on a fallback the full tidyscan time is added on top (in a real implementation its own
OSD run would be shared, so that is pessimistic); a skew tool's angle on a turned page is applied
after the turn (text lines keep their angle modulo 90 degrees under a quarter turn).
"""

from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_deskew as E  # noqa: E402


def load(path: str) -> dict:
    return {r["id"]: r for r in map(json.loads, open(path))} if os.path.exists(path) else {}


def main(work: str, extra: str) -> None:
    cases = {c["id"]: c for c in map(json.loads, open(os.path.join(work, "cases.jsonl")))}
    R = {
        t: load(os.path.join(work, "results", t + ".jsonl"))
        for t in (
            "tidyscan",
            "tidyscan-orientation",
            "osd",
            "rapid-orientation",
            "jdeskew-45",
            "cv-hough",
            "leptonica",
        )
    }
    med = {t: statistics.median(r["seconds"] for r in v.values()) for t, v in R.items() if v}
    timed = {t: set(v) for t, v in R.items()}
    for t in ("jdeskew-45", "cv-hough", "leptonica"):
        for i, r in load(os.path.join(extra, t + ".jsonl")).items():
            R[t].setdefault(i, r)

    def sec(t: str, i: str) -> float:
        return R[t][i]["seconds"] if i in timed[t] else med[t]

    def agree(a: float, b: float, tol: float = 0.5) -> bool:
        return abs(E.norm(a - b, 45.0)) <= tol

    # Each pipeline: case id -> (turn, angle, seconds, fell_back)
    def full(i):
        r = R["tidyscan"][i]
        return r.get("turn") or 0, r.get("angle") or 0.0, r["seconds"], True

    def fast_orient(i, second: str):
        o, s = R["osd"][i], R[second][i]
        t = sec("osd", i) + sec(second, i)
        return (o.get("turn") or 0) if (o.get("turn") or 0) == (s.get("turn") or 0) else None, t

    def fast_skew(i, check: str | None):
        j = R["jdeskew-45"][i].get("angle") or 0.0
        t = sec("jdeskew-45", i)
        if check is None:
            return j, t
        k = R[check][i].get("angle") or 0.0
        return (j if agree(j, k) else None), t + sec(check, i)

    def pipe(orient_second: str | None, skew_check: str | None, fallback: str = "full"):
        def f(i):
            t_spent = 0.0
            if orient_second:
                turn, dt = fast_orient(i, orient_second)
                t_spent += dt
            else:
                turn = 0
            ang, dt = fast_skew(i, skew_check)
            t_spent += dt
            if turn is None or ang is None:
                ft, fa, fs, _ = full(i)
                if fallback == "full" or turn is None:
                    return ft, fa, t_spent + fs, True
                return turn, fa, t_spent + fs, True  # orientation agreed: keep it, skew from tidyscan
            return turn, ang, t_spent, False

        return f

    pipelines = {
        "tidyscan alone": full,
        "jdeskew-45 alone (no turn)": lambda i: (
            0,
            R["jdeskew-45"][i].get("angle") or 0.0,
            sec("jdeskew-45", i),
            False,
        ),
        "OSD+Rapid agree, jdeskew-45; else tidyscan": pipe("rapid-orientation", None),
        "OSD+Rapid agree, jdeskew-45 ~ cv-hough; else tidyscan": pipe("rapid-orientation", "cv-hough"),
        "OSD+tidyscan-orientation agree, jdeskew-45 ~ cv-hough; else tidyscan": pipe(
            "tidyscan-orientation", "cv-hough"
        ),
        "OSD+Rapid agree, jdeskew-45 ~ leptonica; else tidyscan": pipe("rapid-orientation", "leptonica"),
    }

    ids = [i for i, c in cases.items() if c["group"] in E.KNOWN and not c.get("real") and all(i in R[t] for t in R)]
    print(f"cases: {len(ids)} (every case with an exact answer, mirror grid apart, all tools present)\n")
    print("| pipeline | right (<= 0.5) | made worse | upright | fell back to tidyscan | median s | p90 s | max s |")
    print("|---|---|---|---|---|---|---|---|")
    per_class = {}
    for name, f in pipelines.items():
        rows = []
        for i in ids:
            turn, ang, s, fb = f(i)
            e = E.evaluate(cases[i], {"turn": turn, "angle": ang}, name)
            rows.append((e, s, fb, cases[i]))
        n = len(rows)
        right = sum(e["outcome"] == "right" for e, *_ in rows)
        worse = sum(e["outcome"] == "worse" for e, *_ in rows)
        up = sum(e["upright"] for e, *_ in rows)
        fb = sum(x for _, _, x, _ in rows)
        ts = sorted(s for _, s, _, _ in rows)
        print(
            f"| {name} | {right / n:.0%} | {worse / n:.1%} | {up / n:.0%} | {fb / n:.0%} | "
            f"{statistics.median(ts):.1f} | {ts[int(n * 0.9)]:.1f} | {ts[-1]:.0f} |"
        )
        per_class[name] = rows

    print("\n### per document class: right / made worse\n")
    names = list(pipelines)
    classes = sorted({E._cls(c) for *_, c in per_class[names[0]]})
    print("| class | cases | " + " | ".join(names) + " |\n|---|---|" + "---|" * len(names))
    for cl in classes:
        cells, n = [], 0
        for nm in names:
            rows = [e for e, _, _, c in per_class[nm] if E._cls(c) == cl]
            n = len(rows)
            cells.append(
                f"{sum(e['outcome'] == 'right' for e in rows) / n:.0%} / "
                f"{sum(e['outcome'] == 'worse' for e in rows) / n:.0%}"
            )
        print(f"| {cl} | {n} | " + " | ".join(cells) + " |")

    # Real pages: consistency of the pipeline's answer under a known extra tilt or turn.
    print("\n### real pages (CORD receipts, FUNSD forms): consistent under a known extra tilt / turn\n")
    print("| pipeline | extra tilt within 0.5 | turn upright again |\n|---|---|---|")
    real = [c for c in cases.values() if c.get("real") and c["id"] != c["base"]]
    for name, f in pipelines.items():
        tilt_ok = tilt_n = turn_ok = turn_n = 0
        for c in real:
            if not all(c["id"] in R[t] and c["base"] in R[t] for t in R):
                continue
            bt, ba, _, _ = f(c["base"])
            t, a, _, _ = f(c["id"])
            e = E.evaluate(c, {"turn": t, "angle": a}, name, base_corr=ba - bt)
            if c["quarter"]:
                turn_n += 1
                turn_ok += e["upright"]
            else:
                tilt_n += 1
                tilt_ok += abs(e["after"]) <= 0.5
        print(f"| {name} | {tilt_ok}/{tilt_n} | {turn_ok}/{turn_n} |")
    print(
        "\nTimes: fast stages added up; a fallback adds the full tidyscan time (pessimistic: its own OSD"
        " run could be shared). Skew-tool times on turned pages are the tool's median (those runs overlapped"
        " other work)."
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
