from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from src.agent.config import DEFAULT_PHASE6_LIMITS, Phase6Limits
from src.agent.identity import canonical_json, sha256_hex
from src.agent.backends.persistence.database import SessionFactory
from src.agent.backends.persistence.models import DatasetCatalogRow, utcnow
from src.agent.runtime.tool_runtime import (
    ToolDefinition,
    ToolError,
    ToolExecutionContext,
    ToolReference,
    ToolResult,
)
from src.agent.backends.workspace import WorkspaceService


class DatasetService:
    def __init__(self, session_factory: SessionFactory, workspace: WorkspaceService) -> None:
        self.session_factory = session_factory
        self.workspace = workspace

    def bind(
        self,
        *,
        tenant_id: str,
        task_id: str,
        workspace_path: str,
        operation_key: str,
    ) -> str:
        metadata = self.workspace.metadata(tenant_id, task_id, workspace_path)
        dataset_ref = f"ds_{sha256_hex(f'{task_id}|{metadata.content_hash}')[:40]}"
        with self.session_factory() as session:
            existing = session.scalar(
                select(DatasetCatalogRow).where(
                    DatasetCatalogRow.tenant_id == tenant_id,
                    DatasetCatalogRow.task_id == task_id,
                    DatasetCatalogRow.dataset_ref == dataset_ref,
                )
            )
            if existing is not None:
                if existing.source_content_hash != metadata.content_hash:
                    raise PermissionError("Dataset reference is outside this task")
                if existing.status == "ready":
                    return dataset_ref
        data = self.workspace.read(tenant_id, task_id, workspace_path)
        suffix = workspace_path.lower().rsplit(".", 1)[-1]
        frames: dict[str, pd.DataFrame]
        if suffix == "csv":
            frames = {"data": pd.read_csv(io.BytesIO(data))}
        elif suffix in {"xlsx", "xls"}:
            frames = _unique_sheet_frames(
                pd.read_excel(io.BytesIO(data), sheet_name=None)
            )
        elif suffix in {"parquet", "pq"}:
            frames = {"data": pd.read_parquet(io.BytesIO(data))}
        else:
            raise ValueError("Only CSV, XLSX and Parquet can be bound as Dataset")

        manifest_tables: list[dict[str, Any]] = []
        for index, (name, frame) in enumerate(frames.items()):
            buffer = io.BytesIO()
            frame.to_parquet(buffer, index=False)
            payload = buffer.getvalue()
            if suffix in {"parquet", "pq"} and len(frames) == 1:
                table_path = workspace_path
                table_hash = metadata.content_hash
            else:
                table_path = f"files/data/derived/{dataset_ref}/{index:03d}-{name}.parquet"
                file = self.workspace.publish_bytes(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    path=table_path,
                    data=payload,
                    mime_type="application/vnd.apache.parquet",
                    operation_key=sha256_hex(f"{operation_key}|table:{name}"),
                )
                table_hash = file.content_hash
            manifest_tables.append(
                {
                    "name": name,
                    "workspace_path": table_path,
                    "content_hash": table_hash,
                    "columns": [
                        {"name": str(column), "dtype": str(frame[column].dtype)}
                        for column in frame.columns
                    ],
                    "row_count": int(len(frame)),
                }
            )
        manifest = {"version": 1, "tables": manifest_tables}
        with self.session_factory() as session, session.begin():
            existing = session.scalar(
                select(DatasetCatalogRow).where(
                    DatasetCatalogRow.tenant_id == tenant_id,
                    DatasetCatalogRow.task_id == task_id,
                    DatasetCatalogRow.dataset_ref == dataset_ref,
                )
            )
            if existing is None:
                session.add(
                    DatasetCatalogRow(
                        dataset_ref=dataset_ref,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        source_workspace_path=workspace_path,
                        source_content_hash=metadata.content_hash,
                        status="ready",
                        manifest_json=manifest,
                        operation_key=operation_key,
                    )
                )
            else:
                existing.status = "ready"
                existing.manifest_json = manifest
                existing.updated_at = utcnow()
        return dataset_ref

    def resolve(self, tenant_id: str, task_id: str, dataset_ref: str) -> DatasetCatalogRow:
        with self.session_factory() as session:
            row = session.scalar(
                select(DatasetCatalogRow).where(
                    DatasetCatalogRow.tenant_id == tenant_id,
                    DatasetCatalogRow.task_id == task_id,
                    DatasetCatalogRow.dataset_ref == dataset_ref,
                )
            )
            if row is None:
                raise FileNotFoundError("Dataset was not found")
            session.expunge(row)
            return row

    def list_refs(self, tenant_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(DatasetCatalogRow)
                .where(
                    DatasetCatalogRow.tenant_id == tenant_id,
                    DatasetCatalogRow.task_id == task_id,
                    DatasetCatalogRow.status == "ready",
                )
                .order_by(DatasetCatalogRow.created_at, DatasetCatalogRow.dataset_ref)
            ).all()
            return [
                {
                    "dataset_ref": row.dataset_ref,
                    "workspace_path": row.source_workspace_path,
                    "tables": [
                        item["name"] for item in row.manifest_json.get("tables", [])
                    ],
                }
                for row in rows
            ]

    def table(self, tenant_id: str, task_id: str, dataset_ref: str, name: str | None) -> tuple[dict[str, Any], pd.DataFrame]:
        row = self.resolve(tenant_id, task_id, dataset_ref)
        if row.status != "ready":
            raise RuntimeError("dataset_not_ready")
        tables = row.manifest_json.get("tables", [])
        if name is None:
            if len(tables) != 1:
                raise ValueError("table is required for a multi-table Dataset")
            selected = tables[0]
        else:
            selected = next((item for item in tables if item["name"] == name), None)
            if selected is None:
                raise FileNotFoundError("Dataset table was not found")
        payload = self.workspace.read(tenant_id, task_id, selected["workspace_path"])
        if sha256_hex(payload) != selected["content_hash"]:
            raise RuntimeError("Dataset table integrity check failed")
        return selected, pd.read_parquet(io.BytesIO(payload))


class InspectDatasetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_ref: str


class ProfileDatasetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_ref: str
    table: str | None = None
    columns: list[str] | None = None


class QueryDatasetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_ref: str
    table: str | None = None
    query: str = Field(min_length=1)
    row_limit: int = Field(
        default=DEFAULT_PHASE6_LIMITS.dataset_query_default_rows,
        ge=1,
        le=DEFAULT_PHASE6_LIMITS.dataset_query_max_rows,
    )


class InspectDatasetAdapter:
    def __init__(self, datasets: DatasetService) -> None:
        self.datasets = datasets

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            row = self.datasets.resolve(
                context.tenant_id, context.task_id, arguments["dataset_ref"]
            )
            if row.status != "ready":
                return _dataset_error("conflict", "dataset_not_ready")
            tables = []
            for item in row.manifest_json.get("tables", []):
                _, frame = self.datasets.table(
                    context.tenant_id,
                    context.task_id,
                    arguments["dataset_ref"],
                    item["name"],
                )
                tables.append(
                    {
                        "name": item["name"],
                        "columns": [
                            {
                                "name": str(column),
                                "dtype": str(frame[column].dtype),
                                "null_count": int(frame[column].isna().sum()),
                                "null_ratio": float(frame[column].isna().mean()),
                            }
                            for column in frame.columns
                        ],
                        "row_count": int(len(frame)),
                        "sample_preview": _records(frame.head(5)),
                    }
                )
            return ToolResult(
                status="success",
                summary=f"{len(tables)} logical tables",
                data={"tables": tables, "truncated": False},
            )
        except FileNotFoundError:
            return _dataset_error("not_found", "not_found")
        except Exception as exc:
            return _dataset_error("tool_error", "dataset_parse_failed", str(exc))


class ProfileDatasetAdapter:
    def __init__(
        self,
        datasets: DatasetService,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        self.datasets = datasets
        self.limits = limits

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            _, frame = self.datasets.table(
                context.tenant_id,
                context.task_id,
                arguments["dataset_ref"],
                arguments.get("table"),
            )
            requested = arguments.get("columns")
            if requested is None:
                count = self.limits.dataset_profile_default_columns
                columns = list(frame.columns[:count])
                skipped = list(frame.columns[count:])
            else:
                if len(requested) > self.limits.dataset_profile_max_columns:
                    raise ValueError(
                        "explicit columns exceed the configured Dataset profile limit"
                    )
                unknown = set(requested) - set(frame.columns)
                if unknown:
                    raise ValueError(f"unknown columns: {sorted(unknown)}")
                columns = requested
                skipped = []
            profiles = [_profile(frame[column], str(column)) for column in columns]
            data = {
                "profiles": profiles,
                "columns_profiled": [str(column) for column in columns],
                "columns_skipped": [str(column) for column in skipped],
                "truncated": bool(skipped),
            }
            return ToolResult(
                status="success",
                summary=f"Profiled {len(columns)} columns",
                data=data,
                references=[ToolReference(content_hash=sha256_hex(canonical_json(data)))],
                truncated=bool(skipped),
            )
        except FileNotFoundError:
            return _dataset_error("not_found", "not_found")
        except RuntimeError as exc:
            return _dataset_error("conflict", "dataset_not_ready", str(exc))
        except ValueError as exc:
            return _dataset_error("invalid_argument", "invalid_argument", str(exc))


class QueryDatasetAdapter:
    def __init__(
        self,
        datasets: DatasetService,
        workspace: WorkspaceService,
        limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
    ) -> None:
        self.datasets = datasets
        self.workspace = workspace
        self.limits = limits

    def execute(self, context: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            _validate_dataset_query(arguments["query"])
            _, frame = self.datasets.table(
                context.tenant_id,
                context.task_id,
                arguments["dataset_ref"],
                arguments.get("table"),
            )
            try:
                import duckdb
            except ImportError as exc:  # pragma: no cover - packaging dependency
                raise RuntimeError("duckdb is required for Dataset queries") from exc
            connection = duckdb.connect(database=":memory:", config={"enable_external_access": "false"})
            try:
                connection.register("_bound_frame", frame)
                connection.execute("CREATE TEMP VIEW dataset AS SELECT * FROM _bound_frame")
                limit = int(
                    arguments.get(
                        "row_limit", self.limits.dataset_query_default_rows
                    )
                )
                if not 1 <= limit <= self.limits.dataset_query_max_rows:
                    raise ValueError("row_limit exceeds the configured Dataset limit")
                relation = connection.execute(
                    f"SELECT * FROM ({arguments['query']}) AS _q LIMIT {limit + 1}"
                )
                description = relation.description
                if description is None:
                    raise RuntimeError("Dataset query did not return a result set")
                columns = [item[0] for item in description]
                rows = [list(row) for row in relation.fetchall()]
            finally:
                connection.close()
            truncated = len(rows) > limit
            rows = rows[:limit]
            data = {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
            }
            encoded = canonical_json(data)
            if len(encoded) <= context.inline_budget:
                return ToolResult(
                    status="success",
                    summary=f"{len(rows)} Dataset rows",
                    data=data,
                    references=[ToolReference(content_hash=sha256_hex(encoded))],
                    truncated=truncated,
                )
            stream = io.StringIO()
            writer = __import__("csv").writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
            file = self.workspace.publish_bytes(
                tenant_id=context.tenant_id,
                task_id=context.task_id,
                path=f"files/data/results/{context.operation_key[:24]}.csv",
                data=stream.getvalue().encode("utf-8"),
                mime_type="text/csv; charset=utf-8",
                operation_key=context.operation_key,
            )
            return ToolResult(
                status="success",
                summary=f"{len(rows)} Dataset rows in Workspace",
                references=[
                    ToolReference(
                        workspace_path=file.workspace_path,
                        content_hash=file.content_hash,
                        mime_type=file.mime_type,
                        size=file.size,
                    )
                ],
                truncated=truncated,
            )
        except FileNotFoundError:
            return _dataset_error("not_found", "not_found")
        except PermissionError as exc:
            return _dataset_error("policy_denied", "policy_denied", str(exc))
        except ValueError as exc:
            return _dataset_error("invalid_argument", "invalid_argument", str(exc))
        except RuntimeError as exc:
            code = "dataset_not_ready" if str(exc) == "dataset_not_ready" else "dataset_parse_failed"
            return _dataset_error("conflict" if code == "dataset_not_ready" else "tool_error", code, str(exc))


def tool_definitions(
    datasets: DatasetService,
    workspace: WorkspaceService,
    limits: Phase6Limits = DEFAULT_PHASE6_LIMITS,
) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="inspect_dataset",
            args_schema=InspectDatasetArgs,
            adapter=InspectDatasetAdapter(datasets),
        ),
        ToolDefinition(
            name="profile_dataset",
            args_schema=ProfileDatasetArgs,
            adapter=ProfileDatasetAdapter(datasets, limits),
            evidence_eligible=True,
            effect_target=lambda args: sha256_hex(canonical_json(args)),
        ),
        ToolDefinition(
            name="query_dataset",
            args_schema=QueryDatasetArgs,
            adapter=QueryDatasetAdapter(datasets, workspace, limits),
            evidence_eligible=True,
            effect_target=lambda args: sha256_hex(canonical_json(args)),
        ),
    ]


def _validate_dataset_query(query: str) -> None:
    try:
        from sqlglot import exp, parse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("sqlglot is required for Dataset query validation") from exc
    statements = parse(query, read="duckdb")
    if len(statements) != 1 or not isinstance(
        statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
    ):
        raise ValueError("Dataset query must be one read-only SELECT")
    tree = statements[0]
    forbidden = {
        "delta_scan",
        "glob",
        "httpfs",
        "iceberg_scan",
        "mysql_scan",
        "parquet_scan",
        "postgres_scan",
        "query",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_parquet",
        "read_xlsx",
        "sqlite_scan",
    }
    for function in tree.find_all(exp.Func):
        name = str(getattr(function, "name", "") or function.sql_name()).lower()
        if name in forbidden or name.startswith("pragma_") or name.startswith("duckdb_"):
            raise PermissionError("external file and network functions are denied")
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name not in {"dataset", *ctes}:
            raise PermissionError("Dataset query may only read the bound dataset view")


def _profile(series: pd.Series, name: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "column": name,
        "dtype": str(series.dtype),
        "count": int(series.count()),
        "null_count": int(series.isna().sum()),
        "distinct_count": int(series.nunique(dropna=True)),
    }
    if pd.api.types.is_numeric_dtype(series):
        clean = series.dropna()
        if not clean.empty:
            q25, q50, q75 = (float(value) for value in clean.quantile([0.25, 0.5, 0.75]))
            iqr = q75 - q25
            result.update(
                {
                    "mean": float(clean.mean()),
                    "std": float(clean.std()) if len(clean) > 1 else 0.0,
                    "min": float(clean.min()),
                    "max": float(clean.max()),
                    "quantiles": {"q25": q25, "q50": q50, "q75": q75},
                    "outlier_count": int(
                        ((clean < q25 - 1.5 * iqr) | (clean > q75 + 1.5 * iqr)).sum()
                    ),
                }
            )
    else:
        counts = series.dropna().astype(str).value_counts().head(10)
        result["top_values"] = [
            {"value": value, "count": int(count)} for value, count in counts.items()
        ]
    return result


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): (None if pd.isna(value) else value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _safe_table(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", str(name)).strip("_")
    return value or "sheet"


def _unique_sheet_frames(sheets: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for original_name, frame in sheets.items():
        base = _safe_table(original_name)
        name = base
        suffix = 2
        while name in frames:
            name = f"{base}_{suffix}"
            suffix += 1
        frames[name] = frame
    return frames


def _dataset_error(error_type: str, code: str, message: str | None = None) -> ToolResult:
    return ToolResult(
        status="error",
        error=ToolError(
            type=error_type,  # type: ignore[arg-type]
            code=code,
            message=message or code,
        ),
    )
