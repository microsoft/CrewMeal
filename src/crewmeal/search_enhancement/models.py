from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContentSection:
    heading: str
    paragraphs: tuple[str, ...]
    bullets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentTable:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    key_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChartDataPoint:
    series: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ContentChart:
    title: str
    data_points: tuple[ChartDataPoint, ...]
    insights: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContentRelationship:
    source: str
    relation: str
    target: str
    description: str


@dataclass(frozen=True, slots=True)
class ContentImage:
    description: str
    role: str
    visible_text: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HierarchyRow:
    path: tuple[str, ...]
    note: str


@dataclass(frozen=True, slots=True)
class ContentHierarchy:
    title: str
    level_labels: tuple[str, ...]
    rows: tuple[HierarchyRow, ...]


@dataclass(frozen=True, slots=True)
class ScheduleTask:
    task_path: tuple[str, ...]
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class ScheduleMilestone:
    name: str
    when: str


@dataclass(frozen=True, slots=True)
class SlideSchedule:
    time_axis: tuple[str, ...]
    tasks: tuple[ScheduleTask, ...]
    milestones: tuple[ScheduleMilestone, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.time_axis or self.tasks or self.milestones)


@dataclass(frozen=True, slots=True)
class ContentFlow:
    title: str
    lane: str
    steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SlideContent:
    slide_number: int
    title: str
    summary: str
    facts: tuple[str, ...]
    sections: tuple[ContentSection, ...]
    hierarchies: tuple[ContentHierarchy, ...]
    schedule: SlideSchedule
    flows: tuple[ContentFlow, ...]
    tables: tuple[ContentTable, ...]
    charts: tuple[ContentChart, ...]
    relationships: tuple[ContentRelationship, ...]
    images: tuple[ContentImage, ...]
    warnings: tuple[str, ...]

    @classmethod
    def from_validated_dict(cls, value: dict[str, Any]) -> "SlideContent":
        tables = tuple(
            ContentTable(
                title=table["title"],
                headers=tuple(table["headers"]),
                rows=tuple(tuple(row) for row in table["rows"]),
                key_facts=tuple(table["keyFacts"]),
            )
            for table in value["tables"]
        )
        for table in tables:
            if any(len(row) != len(table.headers) for row in table.rows):
                raise ValueError(
                    f"Slide {value['slideNumber']} table rows must match header width."
                )

        return cls(
            slide_number=value["slideNumber"],
            title=value["title"].strip(),
            summary=value["summary"].strip(),
            facts=tuple(
                fact.strip() for fact in value["facts"] if fact.strip()
            ),
            sections=tuple(
                ContentSection(
                    heading=section["heading"].strip(),
                    paragraphs=tuple(section["paragraphs"]),
                    bullets=tuple(section["bullets"]),
                )
                for section in value["sections"]
            ),
            hierarchies=tuple(
                ContentHierarchy(
                    title=hierarchy["title"].strip(),
                    level_labels=tuple(hierarchy["levelLabels"]),
                    rows=tuple(
                        HierarchyRow(
                            path=tuple(row["path"]),
                            note=row["note"].strip(),
                        )
                        for row in hierarchy["rows"]
                        if any(cell.strip() for cell in row["path"])
                    ),
                )
                for hierarchy in value["hierarchies"]
            ),
            schedule=SlideSchedule(
                time_axis=tuple(value["schedule"]["timeAxis"]),
                tasks=tuple(
                    ScheduleTask(
                        task_path=tuple(task["taskPath"]),
                        start=task["start"].strip(),
                        end=task["end"].strip(),
                    )
                    for task in value["schedule"]["tasks"]
                    if any(cell.strip() for cell in task["taskPath"])
                ),
                milestones=tuple(
                    ScheduleMilestone(
                        name=milestone["name"].strip(),
                        when=milestone["when"].strip(),
                    )
                    for milestone in value["schedule"]["milestones"]
                    if milestone["name"].strip()
                ),
            ),
            flows=tuple(
                ContentFlow(
                    title=flow["title"].strip(),
                    lane=flow["lane"].strip(),
                    steps=tuple(step for step in flow["steps"] if step.strip()),
                )
                for flow in value["flows"]
                if any(step.strip() for step in flow["steps"])
            ),
            tables=tables,
            charts=tuple(
                ContentChart(
                    title=chart["title"].strip(),
                    data_points=tuple(
                        ChartDataPoint(
                            series=point["series"],
                            label=point["label"],
                            value=point["value"],
                        )
                        for point in chart["dataPoints"]
                    ),
                    insights=tuple(chart["insights"]),
                )
                for chart in value["charts"]
            ),
            relationships=tuple(
                ContentRelationship(
                    source=relationship["source"],
                    relation=relationship["relation"],
                    target=relationship["target"],
                    description=relationship["description"],
                )
                for relationship in value["relationships"]
            ),
            images=tuple(
                ContentImage(
                    description=image["description"],
                    role=image["role"],
                    visible_text=tuple(image["visibleText"]),
                )
                for image in value["images"]
            ),
            warnings=tuple(value["warnings"]),
        )


@dataclass(frozen=True, slots=True)
class StructuredAnalysisResult:
    source_name: str
    slides: tuple[SlideContent, ...]
    usage: dict[str, Any]
    raw_result: dict[str, Any]
    warnings: tuple[dict[str, Any], ...]
    analysis_seconds: float


@dataclass(frozen=True, slots=True)
class RenderedHtml:
    content: str
    byte_count: int
    sha256: str
    slide_titles: tuple[str, ...]
    keywords: tuple[str, ...]
