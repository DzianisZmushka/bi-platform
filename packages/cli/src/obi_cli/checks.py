"""Project validation for the CLI (ADR-008).

Static (default): obi_formats resolves every name and reads columns from catalog.json.
No data access, so CI can run it.

--execute: additionally compares catalog.json with the live source and runs every measure
once on the data. That catches what static checks cannot, such as `year()` applied to a
text column, and reports it as file:line naming the measure, never as a raw database error.
"""

from __future__ import annotations

from pathlib import Path

from obi_engine import EngineError, Project
from obi_engine.store import auto_migrate
from obi_formats import catalog as catalog_mod
from obi_formats.artifacts import parse
from obi_formats.validate import Issue, validate_project


def validate_all(root: str | Path, execute: bool = False) -> list[Issue]:
    root = Path(root)
    issues = validate_project(root)
    if not execute or any(i.severity == "error" for i in issues):
        return issues  # running measures on a model with known errors only repeats them
    try:
        project = Project(root)
    except Exception as e:  # a data file DuckDB cannot read
        return issues + [Issue("error", "project.yaml", 1, f"project `{root.name}`", None,
                               f"the data could not be loaded: {_short(e)}")]
    try:
        auto_migrate(project)  # the database follows the model, as `obi migrate` would
    except Exception as e:
        return issues + [Issue("error", "project.yaml", 1, f"project `{root.name}`", None,
                               f"the database could not be migrated: {_short(e)}")]
    cached = catalog_mod.load(root)
    if cached is not None:
        changes = catalog_mod.drift(cached, project.describe())
        if changes:
            issues.append(Issue("error", catalog_mod.CATALOG_FILE, 1, f"project `{root.name}`", None,
                                "catalog.json is out of date: " + "; ".join(changes)
                                + ". Run `obi catalog` and commit the result"))
    for path in sorted(root.glob("model/*.model.yaml")):
        rel = path.relative_to(root).as_posix()
        doc = parse(path.read_text(encoding="utf-8"))
        model = doc.data["model"]
        try:
            eng = project.engine(model)
            problems = eng.check_measures()
            rates = eng.check_rates()
            lookups = eng.check_lookups()
        except EngineError as e:
            issues.append(Issue("error", rel, 1, f"model `{model}`", None, e.args[0]))
            continue
        for entity, message in rates:
            line = doc.line_of(("entities", entity, "currency"))
            issues.append(Issue("error", rel, line, f"model `{model}`", f"entity `{entity}`", message))
        for field, message in lookups:
            line = doc.line_of(("entities", field.entity, "dimensions", field.name))
            issues.append(Issue("error", rel, line, f"model `{model}`", f"dimension `{field.name}`", message))
        for field, message in problems:
            line = doc.line_of(("entities", field.entity, "measures", field.name))
            issues.append(Issue("error", rel, line, f"model `{model}`", f"measure `{field.name}`", message))
    return sorted(issues, key=lambda i: (i.file, i.line or 0))


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:200]
