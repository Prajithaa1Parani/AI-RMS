"""Pydantic schemas for every structured AI output.

An AI response is only used after it passes one of these models.
`extra="forbid"` means unknown fields are a validation error too.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

import config

Category = Literal["hostel", "fees", "examination", "academics", "it_services",
                   "library", "transport", "mess_food", "placement", "other"]
Priority = Literal["low", "medium", "high", "critical"]
Department = Literal["hostel_office", "accounts", "examination_cell", "academic_affairs",
                     "it_helpdesk", "library", "transport_office", "mess_committee",
                     "placement_cell", "student_welfare"]
Action = Literal["assign_to_department", "escalate_to_admin", "approve_refund", "reject_invalid"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChunkSummary(Strict):
    summary: str = Field(min_length=10, max_length=1500)


class Contradiction(Strict):
    field: str = Field(min_length=1, max_length=60)
    values: List[str] = Field(min_length=2, max_length=6)
    explanation: str = Field(min_length=5, max_length=300)


class ExtractedFields(Strict):
    location: Optional[str] = Field(default=None, max_length=120)
    incident_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    course_code: Optional[str] = Field(default=None, max_length=20)
    reference_id: Optional[str] = Field(default=None, max_length=60)


class Extraction(Strict):
    """Stage 1: what the complaint actually says (no decisions yet)."""
    issue_summary: str = Field(min_length=10, max_length=400)
    category: Category
    fields: ExtractedFields
    is_actionable: bool
    clarifying_questions: List[str] = Field(default_factory=list, max_length=4)
    contradictions: List[Contradiction] = Field(default_factory=list, max_length=5)
    information_requests: List[str] = Field(default_factory=list, max_length=5)
    injection_suspected: bool


class Classification(Strict):
    """Stage 2: the routing decision. Produced by two models independently."""
    category: Category
    priority: Priority
    department: Department
    recommended_action: Action
    refund_amount: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=5, max_length=400)


class ResponseDraft(Strict):
    """Stage 3: a suggested reply for staff, grounded in the given context only."""
    response_text: str = Field(min_length=20, max_length=1500)
    unavailable_information: List[str] = Field(default_factory=list, max_length=6)

    @field_validator("response_text")
    @classmethod
    def no_markdown_fences(cls, v):
        if "```" in v:
            raise ValueError("response_text must be plain text")
        return v


# Fields compared between the primary model and the verifier.
COMPARE_FIELDS = ["category", "priority", "department", "recommended_action"]

assert set(config.CATEGORIES) == set(Category.__args__)
assert set(config.DEPARTMENTS) == set(Department.__args__)
