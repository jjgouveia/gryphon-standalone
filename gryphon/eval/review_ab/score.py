"""Turn judgments into per-review scores, per-arm means and a Markdown report.

Metrics per review:

- ``precision``: real findings / (real + false); unverifiable ones are left out.
- ``pooled_recall``: distinct real issues it found / real issues any review of
  the case found. Without a ground truth this is the recall the pool allows.
- ``weighted_recall``: the same, weighting issues by the judged severity
  (blocker 3, major 2, minor 1).
- ``known_recall``: known issues it found / known issues of the case, when
  the case has any.
- the five rubric scores, 1 to 5.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .judge import RUBRIC
from .sandbox import ARMS

SEVERITY_WEIGHT = {"blocker": 3, "major": 2, "minor": 1, "none": 0}
# Every arm the harness knows, in declaration order: a hard-coded list here
# silently dropped arms added later from the summary and the issue matrix.
ARM_ORDER = ARMS


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 3) if den else None


def score_case(payload: dict, labels: dict, records: list[dict], known_ids: list[str]) -> dict:
    """Rows per review and the issue matrix for one judged case."""
    judgment = payload["judgment"]
    issues = judgment["issues"]
    by_ref: dict[str, dict] = {}
    for issue in issues:
        for ref in issue["reported_by"]:
            by_ref[f"{ref['review']}:F{ref['finding']}"] = issue
    real = {i["id"]: i for i in issues if i["verdict"] == "real"}
    # Ground truth: the person-confirmed ids plus the candidates the judge
    # confirmed from their later diff.
    judge_confirmed = {
        v["id"] for v in judgment.get("known_verdicts", []) if v.get("is_defect")
    }
    known_ids = sorted(set(known_ids) | judge_confirmed)
    total_weight = sum(SEVERITY_WEIGHT[i["severity"]] for i in real.values())
    scores = {r["review"]: r for r in judgment["reviews"]}
    record_by_stream = {r["stream"]: r for r in records}

    rows = []
    for label, meta in sorted(labels.items(), key=lambda kv: int(kv[0][1:])):
        rec = record_by_stream.get(meta["stream"], {})
        findings = rec.get("findings") or []
        verdicts = [by_ref.get(f"{label}:F{i}", {}).get("verdict") for i in range(len(findings))]
        n_real = verdicts.count("real")
        n_false = verdicts.count("false")
        found_real = {
            by_ref[f"{label}:F{i}"]["id"]
            for i in range(len(findings))
            if by_ref.get(f"{label}:F{i}", {}).get("verdict") == "real"
        }
        found_known = {real[i]["known_issue"] for i in found_real if real[i]["known_issue"]}
        weight = sum(SEVERITY_WEIGHT[real[i]["severity"]] for i in found_real)
        row = {
            "case_id": payload["case_id"],
            "label": label,
            "arm": meta["arm"],
            "rep": meta["rep"],
            "findings": len(findings),
            "real": n_real,
            "false": n_false,
            "unverifiable": verdicts.count("unverifiable"),
            "unassigned": verdicts.count(None),
            "precision": _ratio(n_real, n_real + n_false),
            "pooled_recall": _ratio(len(found_real), len(real)),
            "weighted_recall": _ratio(weight, total_weight),
            "known_recall": _ratio(len(found_known), len(known_ids)) if known_ids else None,
            "cost_usd": rec.get("total_cost_usd"),
            "turns": rec.get("num_turns"),
            "wall_seconds": rec.get("wall_seconds"),
            "graph_tool_calls": rec.get("graph_tool_calls"),
            "protocol_ok": rec.get("protocol_ok"),
            **{k: scores.get(label, {}).get("scores", {}).get(k) for k in RUBRIC},
            "judge_comment": scores.get(label, {}).get("comment"),
        }
        rows.append(row)

    matrix = []
    for issue in issues:
        found_by: dict[str, set[int]] = {}
        for ref in issue["reported_by"]:
            meta = labels.get(ref["review"])
            if meta:
                found_by.setdefault(meta["arm"], set()).add(meta["rep"])
        matrix.append({
            "id": issue["id"],
            "title": issue["title"],
            "file": issue["file"],
            "verdict": issue["verdict"],
            "severity": issue["severity"],
            "known_issue": issue["known_issue"],
            "rationale": issue["rationale"],
            "found_by": {arm: len(reps) for arm, reps in found_by.items()},
        })
    reps_per_arm: dict[str, int] = {}
    for meta in labels.values():
        reps_per_arm[meta["arm"]] = reps_per_arm.get(meta["arm"], 0) + 1
    return {
        "rows": rows, "matrix": matrix, "reps_per_arm": reps_per_arm,
        "known_ids": known_ids, "judge_confirmed": sorted(judge_confirmed),
        "known_verdicts": judgment.get("known_verdicts", []),
    }


_MEAN_FIELDS = (
    "findings", "real", "false", "unverifiable", "precision", "pooled_recall",
    "weighted_recall", "known_recall", *RUBRIC, "cost_usd", "turns", "wall_seconds",
    "graph_tool_calls",
)


def aggregate_by_arm(rows: list[dict]) -> dict[str, dict]:
    """Mean of every metric per arm, ignoring missing values."""
    out: dict[str, dict] = {}
    for arm in ARM_ORDER:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if not arm_rows:
            continue
        agg: dict = {"reviews": len(arm_rows)}
        for key in _MEAN_FIELDS:
            values = [r[key] for r in arm_rows if isinstance(r.get(key), (int, float))]
            agg[key] = round(statistics.mean(values), 3) if values else None
        out[arm] = agg
    return out


def load_run(run_dir: Path, known_by_case: dict[str, list[str]]) -> dict:
    """Score every judged case in *run_dir*."""
    records = [
        json.loads(line)
        for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = {}
    judges = []
    for path in sorted((run_dir / "judgments").glob("*.json")):
        if path.name.endswith(".labels.json"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        judges.append(payload)
        if payload.get("status") != "ok" or not payload.get("judgment"):
            continue
        labels = json.loads(
            (run_dir / "judgments" / f"{payload['case_id']}.labels.json").read_text(
                encoding="utf-8"
            )
        )
        case_records = [r for r in records if r["case_id"] == payload["case_id"]]
        cases[payload["case_id"]] = score_case(
            payload, labels, case_records, known_by_case.get(payload["case_id"], []),
        )
    rows = [row for c in cases.values() for row in c["rows"]]
    return {
        "run": json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
        "records": records,
        "judges": judges,
        "cases": cases,
        "rows": rows,
        "by_arm": aggregate_by_arm(rows),
    }


def load_runs(run_dirs: list[Path], known_by_case: dict[str, list[str]]) -> dict:
    """Pool several runs into one scored result (runs stopped and resumed).

    A case judged in more than one run keeps both, keyed ``case (run)``.
    """
    loaded = [load_run(d, known_by_case) for d in run_dirs]
    if len(loaded) == 1:
        return loaded[0]
    cases: dict[str, dict] = {}
    for scored in loaded:
        for case_id, case in scored["cases"].items():
            key = case_id if case_id not in cases else f"{case_id} ({scored['run']['run_id']})"
            cases[key] = case
    rows = [row for scored in loaded for row in scored["rows"]]

    def same(key: str):
        values = {str(s["run"].get(key)) for s in loaded}
        return values.pop() if len(values) == 1 else "vários"

    run = {
        "run_id": " + ".join(s["run"]["run_id"] for s in loaded),
        "model": same("model"), "effort": same("effort"),
        "prompt_version": same("prompt_version"), "reps": same("reps"),
        "cases": [c for s in loaded for c in s["run"]["cases"]],
    }
    return {
        "run": run,
        "records": [r for s in loaded for r in s["records"]],
        "judges": [j for s in loaded for j in s["judges"]],
        "cases": cases,
        "rows": rows,
        "by_arm": aggregate_by_arm(rows),
    }


def _fmt(value, pct: bool = False) -> str:
    if value is None:
        return "—"
    if pct:
        return f"{value * 100:.0f}%"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _usd(value) -> str:
    return "—" if value is None else f"US$ {value:.2f}"


def render_markdown(scored: dict) -> str:
    """The report: arm summary, then per case the reviews and the issue matrix."""
    run = scored["run"]
    lines = [
        f"# Review A/B — run {run['run_id']}",
        "",
        f"Revisor: `{run['model']}` (effort {run['effort']}), prompt v{run['prompt_version']}, "
        f"{run['reps']} repetição(ões) por braço, casos: {', '.join(run['cases'])}.",
    ]
    judged = [j for j in scored["judges"] if j.get("status") == "ok"]
    if judged:
        cost = sum(j.get("total_cost_usd") or 0 for j in judged)
        lines.append(
            f"Juiz: `{judged[0]['judge_model']}` (v{judged[0]['judge_version']}), "
            f"{len(judged)} caso(s), US$ {cost:.2f}."
        )
    excluded = [r for r in scored["records"] if r["status"] != "ok" or r["leak_flags"]]
    if excluded:
        lines.append(
            "Excluídas do julgamento: "
            + ", ".join(f"{r['case_id']}/{r['arm']}/r{r['rep']} ({r['status']}, "
                        f"{len(r['leak_flags'])} vazamento(s))" for r in excluded)
            + "."
        )

    lines += ["", "## Por braço (médias)", ""]
    header = (
        "| Braço | Revisões | Achados | Reais | Falsos | Precisão | Recall combinado "
        "| Recall ponderado | Recall gabarito | Corretude | Impacto | Testes | Sinal "
        "| Acionável | Custo | Turnos | Tempo (s) | Chamadas ao grafo |"
    )
    lines += [header, "|" + "---|" * 18]
    for arm, a in scored["by_arm"].items():
        lines.append(
            f"| {arm} | {a['reviews']} | {_fmt(a['findings'])} | {_fmt(a['real'])} "
            f"| {_fmt(a['false'])} | {_fmt(a['precision'], True)} "
            f"| {_fmt(a['pooled_recall'], True)} | {_fmt(a['weighted_recall'], True)} "
            f"| {_fmt(a['known_recall'], True)} "
            + " ".join(f"| {_fmt(a[k])}" for k in RUBRIC)
            + f" | {_usd(a['cost_usd'])} | {_fmt(a['turns'])} | {_fmt(a['wall_seconds'])} "
            f"| {_fmt(a['graph_tool_calls'])} |"
        )

    for case_id, case in scored["cases"].items():
        lines += ["", f"## {case_id}", "", "### Revisões", ""]
        lines += [
            "| Rótulo | Braço | Rep | Achados | Reais | Falsos | Precisão | Recall combinado "
            "| Recall gabarito | Corretude | Impacto | Testes | Sinal | Acionável | Custo "
            "| Grafo | Protocolo |",
            "|" + "---|" * 17,
        ]
        for r in case["rows"]:
            protocol = {True: "ok", False: "**violado**", None: "—"}[r["protocol_ok"]]
            lines.append(
                f"| {r['label']} | {r['arm']} | {r['rep']} | {r['findings']} | {r['real']} "
                f"| {r['false']} | {_fmt(r['precision'], True)} "
                f"| {_fmt(r['pooled_recall'], True)} | {_fmt(r['known_recall'], True)} "
                + " ".join(f"| {_fmt(r[k])}" for k in RUBRIC)
                + f" | {_usd(r['cost_usd'])} | {_fmt(r['graph_tool_calls'])} | {protocol} |"
            )
        arms = [a for a in ARM_ORDER if a in case["reps_per_arm"]]
        lines += ["", "### Issues (quantas repetições de cada braço acharam)", ""]
        lines += [
            "| Issue | Veredito | Severidade | Arquivo | "
            + " | ".join(f"{a} (de {case['reps_per_arm'][a]})" for a in arms)
            + " | Descrição |",
            "|" + "---|" * (5 + len(arms)),
        ]
        order = {"real": 0, "unverifiable": 1, "false": 2}
        for m in sorted(
            case["matrix"],
            key=lambda m: (order[m["verdict"]], -SEVERITY_WEIGHT[m["severity"]]),
        ):
            cells = " | ".join(str(m["found_by"].get(a, 0)) for a in arms)
            lines.append(
                f"| {m['id']} | {m['verdict']} | {m['severity']} | `{m['file']}` | {cells} "
                f"| {m['title']} |"
            )
        verdicts = case.get("known_verdicts") or []
        if case.get("known_ids") or verdicts:
            lines += ["", "### Gabarito", ""]
            lines.append(
                f"{len(case.get('known_ids') or [])} bug(s) conhecido(s) contam para o recall, "
                f"{len(case.get('judge_confirmed') or [])} confirmado(s) pelo juiz a partir "
                "do diff do fix."
            )
            for v in verdicts:
                mark = "defeito" if v.get("is_defect") else "não é defeito"
                lines.append(f"- **{v['id']}** ({mark}): {v.get('rationale', '')}")
        lines += ["", "### Justificativas do juiz", ""]
        for m in case["matrix"]:
            lines.append(f"- **{m['id']}** ({m['verdict']}): {m['rationale']}")
        for r in case["rows"]:
            if r["judge_comment"]:
                lines.append(f"- **{r['label']}** ({r['arm']} r{r['rep']}): {r['judge_comment']}")
    lines.append("")
    return "\n".join(lines)
