from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TextSegment(ReportModel):
    type: Literal["text"] = "text"
    text: str


class ClaimSegment(ReportModel):
    type: Literal["claim"] = "claim"
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    citation_ids: list[str]


Segment = Annotated[TextSegment | ClaimSegment, Field(discriminator="type")]


class ListItem(ReportModel):
    segments: list[Segment] = Field(min_length=1)


class ParagraphBlock(ReportModel):
    type: Literal["paragraph"] = "paragraph"
    segments: list[Segment] = Field(min_length=1)


class ListBlock(ReportModel):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[ListItem] = Field(min_length=1)


class TableBlock(ReportModel):
    type: Literal["table"] = "table"
    caption: str | None = None
    columns: list[str] = Field(min_length=1)
    rows: list[list[str]]
    citation_ids: list[str]

    @model_validator(mode="after")
    def validate_table(self) -> "TableBlock":
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("table columns must be unique")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table row width must match columns")
        return self


class ImageBlock(ReportModel):
    type: Literal["image"] = "image"
    workspace_path: str = Field(min_length=1)
    content_hash: str = Field(min_length=32)
    alt_text: str = Field(min_length=1)
    caption: str | None = None
    citation_ids: list[str]


Block = Annotated[
    ParagraphBlock | ListBlock | TableBlock | ImageBlock,
    Field(discriminator="type"),
]


class Section(ReportModel):
    section_id: str = Field(min_length=1)
    heading: str = Field(min_length=1)
    level: int = Field(ge=1, le=6)
    blocks: list[Block] = Field(min_length=1)


class Citation(ReportModel):
    citation_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    locator: str | None = None


class ReportAst(ReportModel):
    report_version: int = Field(ge=1)
    title: str = Field(min_length=1)
    sections: list[Section] = Field(min_length=1)
    citations: list[Citation]


class CitationValidationIssue(ReportModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    claim_id: str | None = None
    citation_id: str | None = None
    evidence_id: str | None = None


class CitationValidationResult(ReportModel):
    valid: bool
    issues: list[CitationValidationIssue]
