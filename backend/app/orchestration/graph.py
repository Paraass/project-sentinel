"""LangGraph workflow skeleton over the durable Batch 6 backbone.

LangGraph here is purely a sequencing layer. It decides which node runs
next; workflow_service remains the only thing that validates transitions
and persists them — this module never touches WorkflowRun/StageCheckpoint
rows directly, and never encodes its own notion of "what comes after what"
beyond calling get_resume_stage.

Deliberately no LangGraph checkpointer is configured. Durability is already
handled by Postgres via workflow_service (proved in Batch 6); adding a
second, LangGraph-native persistence mechanism on top would duplicate
exactly the logic that already exists. Every node re-derives what to do
from persisted state on each invocation, not from anything LangGraph itself
remembers between runs — so "resuming" a graph run after a restart just
means invoking it again with the same run_id and letting routing figure out
where to continue.

Only two real nodes exist: intake and processing. This mirrors the only
states workflow_service currently knows about (INTAKE_PENDING, PROCESSING,
COMPLETED, FAILED). Later Build Orders will replace `processing` with real
movement-level nodes (classify, extract, consolidate, ...) as those states
are added to workflow_service — this graph is the seed those extend, not a
placeholder for the full cold-start/incremental graph from the system
architecture, which would require states that don't exist in the
persistence layer yet.
"""
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.workflow_service import (
    complete_stage,
    create_run,
    fail_stage,
    find_in_progress_checkpoint,
    get_resume_stage,
    start_stage,
)
from app.persistence.models import Document, WorkflowState
from app.storage.document_storage import DocumentInput


class SentinelGraphState(TypedDict, total=False):
    """Data carried between nodes for a single graph invocation.

    Only `run_id` matters across a restart — a resumed invocation is called
    with the same run_id it was given before, and everything else here is
    scratch space for that one invocation, not itself durable. `documents`
    is only meaningful when starting a brand new run (no run_id yet).
    """

    run_id: str
    documents: list[DocumentInput]
    error: str | None


async def _intake_node(state: SentinelGraphState, *, session: AsyncSession) -> SentinelGraphState:
    """Create the run and register its documents.

    Only ever reached for a brand new run (see `_route_entry` below) — a
    resumed invocation always already has a run_id, so this node is skipped
    by routing, not by a check inside the node itself.
    """
    run = await create_run(session, documents=state.get("documents", []))
    await session.commit()
    return {**state, "run_id": str(run.id)}


async def _processing_node(
    state: SentinelGraphState, *, session: AsyncSession
) -> SentinelGraphState:
    """The single placeholder processing stage.

    No real analysis exists yet — classification, extraction, and the rest
    are later Build Orders. This node's only job is to prove the graph can
    start a stage, do deterministic work, and durably complete it through
    the existing service, exactly the shape a real future node will follow.

    Resume-safe: if PROCESSING was already started (an IN_PROGRESS
    checkpoint exists — the "killed mid-stage" case), this does not call
    start_stage again, since PROCESSING -> PROCESSING isn't a transition
    workflow_service allows. It goes straight to finishing the work.
    """
    run_id = uuid.UUID(state["run_id"])

    try:
        already_started = await find_in_progress_checkpoint(
            session, run_id, WorkflowState.PROCESSING
        )
        if already_started is None:
            await start_stage(session, run_id, WorkflowState.PROCESSING)
            await session.commit()

        # Explicit count query rather than accessing a lazy-loaded
        # run.documents relationship: lazy relationship loading relies on
        # SQLAlchemy's implicit-await greenlet trick, which assumes a
        # direct async call chain. LangGraph invokes nodes through its own
        # execution machinery, which doesn't preserve that context — an
        # explicit query has no such dependency and is the more correct
        # pattern here regardless.
        document_count = (
            await session.execute(
                select(func.count()).select_from(Document).where(Document.run_id == run_id)
            )
        ).scalar_one()
        # Deterministic placeholder output — not the eventual real
        # behavior, just proof the node did something and durably recorded
        # it, per the assignment's "never bluffs" requirement.
        output = {"documents_seen": document_count}

        await complete_stage(session, run_id, WorkflowState.PROCESSING, output_data=output)
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - any failure here must be durably recorded, not swallowed
        await fail_stage(session, run_id, WorkflowState.PROCESSING, error_message=str(exc))
        await session.commit()
        return {**state, "error": str(exc)}

    return state


def build_graph(session: AsyncSession) -> CompiledStateGraph:
    """Build and compile the graph, bound to one caller-owned session.

    The session is captured via closure rather than threaded through
    LangGraph's config/state machinery — simplest option for the two nodes
    that exist today, and keeps node signatures the standard
    `(state) -> state` shape.
    """

    async def intake(state: SentinelGraphState) -> SentinelGraphState:
        return await _intake_node(state, session=session)

    async def processing(state: SentinelGraphState) -> SentinelGraphState:
        return await _processing_node(state, session=session)

    async def route_entry(state: SentinelGraphState) -> str:
        """Decide where a graph invocation should actually start.

        - No run_id yet -> brand new run -> intake.
        - run_id given, resume_stage is PROCESSING -> skip intake (already
          durably complete), go straight to processing, whether that means
          starting it fresh or finishing an interrupted attempt.
        - run_id given, resume_stage is COMPLETED/FAILED -> nothing to do,
          end immediately. A completed run is never re-processed.
        """
        if not state.get("run_id"):
            return "intake"

        resume_stage = await get_resume_stage(session, uuid.UUID(state["run_id"]))
        if resume_stage in (WorkflowState.COMPLETED, WorkflowState.FAILED):
            return "end"
        return "processing"

    graph = StateGraph(SentinelGraphState)
    graph.add_node("intake", intake)
    graph.add_node("processing", processing)

    graph.add_conditional_edges(
        START,
        route_entry,
        {"intake": "intake", "processing": "processing", "end": END},
    )
    graph.add_edge("intake", "processing")
    graph.add_edge("processing", END)

    return graph.compile()


async def run_workflow(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    documents: list[DocumentInput] | None = None,
) -> SentinelGraphState:
    """Convenience entry point: build the graph and invoke it once.

    Pass `run_id` to resume an existing run (documents are ignored in that
    case, since intake is skipped). Pass `documents` with no `run_id` to
    start a brand new one.
    """
    graph = build_graph(session)
    initial_state: SentinelGraphState = {}
    if run_id is not None:
        initial_state["run_id"] = str(run_id)
    if documents is not None:
        initial_state["documents"] = documents

    return await graph.ainvoke(initial_state)
