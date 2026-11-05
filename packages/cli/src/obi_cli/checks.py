"""Full project validation for the CLI: static checks, then a run of every measure.

The static validator (obi_formats) resolves every name. Running each measure once on the
data catches what static checks cannot, such as `year()` applied to a text column, and
reports it as file:line naming the measure, never as a raw database error (ADR-008).
"""

from __future__ import annotations

from pathlib import Path

from obi_engine import EngineError, Project
from obi_formats.artifacts import parse
from obi_formats.validate import Issue, validate_project


def validate_all(root: str | Path) -> list[Issue]:
    root = Path(root)
    issues = validate_project(root)
    if any(i.severity == "error" for i in issues):
        return issues  # running measures on a model with known errors only repeats them
    try:
        project = Project(root)
    except Exception as e:  # a data file DuckDB cannot read
        return issues + [Issue("error", "project.yaml", 1, f"project `{root.name}`", None,
                               f"the data could not be loaded: {_short(e)}")]
    for path in sorted(root.glob("model/*.model.yaml")):
        rel = path.relative_to(root).as_posix()
        doc = parse(path.read_text(encoding="utf-8"))
        model = doc.data["model"]
        try:
            problems = project.engine(model).check_measures()
        except EngineError as e:
            issues.append(Issue("error", rel, 1, f"model `{model}`", None, e.args[0]))
            continue
        for field, message in problems:
            line = doc.line_of(("entities", field.entity, "measures", field.name))
            issues.append(Issue("error", rel, line, f"model `{model}`", f"measure `{field.name}`", message))
    return sorted(issues, key=lambda i: (i.file, i.line or 0))


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:200]
