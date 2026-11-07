"""Excel / CSV parsing into a compact text table for the LLM."""

from __future__ import annotations

import io

from app.core.errors import UnsupportedDocumentError
from app.ingestion.parsers.base import DocKind, ParsedDocument, truncate

MAX_ROWS = 500


def _frame_to_text(df) -> str:  # type: ignore[no-untyped-def]
    df = df.dropna(how="all").dropna(axis=1, how="all").head(MAX_ROWS)
    df = df.fillna("")
    lines = [" | ".join(str(c) for c in df.columns)]
    lines += [" | ".join(str(v) for v in row) for row in df.itertuples(index=False)]
    return "\n".join(lines)


def parse_spreadsheet(content: bytes) -> ParsedDocument:
    import pandas as pd

    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001 - pandas raises many types
        raise UnsupportedDocumentError(f"Could not read spreadsheet: {exc}") from exc
    parts = [f"--- Sheet: {name} ---\n{_frame_to_text(df)}" for name, df in sheets.items()]
    return ParsedDocument(kind=DocKind.spreadsheet, text=truncate("\n\n".join(parts)), pages=len(sheets))


def parse_csv(content: bytes) -> ParsedDocument:
    import pandas as pd

    for encoding in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(content), header=None, sep=None, engine="python", encoding=encoding)
            return ParsedDocument(kind=DocKind.csv, text=truncate(_frame_to_text(df)))
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedDocumentError(f"Could not read CSV: {exc}") from exc
    raise UnsupportedDocumentError("Could not decode CSV")
