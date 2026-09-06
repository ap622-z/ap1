"""运行时容器：把引擎/向量库/嵌入/提供方/Agent/Worker 组装为单例。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent import Agent
from app.capabilities import CapabilityRunner
from app.config import Settings
from app.embeddings import Embedder, build_embedder
from app.mailbox import Mailbox
from app.providers import LLMProvider, WebSearchProvider
from app.repository import Repository
from app.vector_store import VectorStore
from app.worker import Worker


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    embedder: Embedder
    vector: VectorStore
    llm: LLMProvider
    search: WebSearchProvider
    repo: Repository
    runner: CapabilityRunner
    agent: Agent
    worker: Worker
    mailbox: Mailbox

    async def aclose(self) -> None:
        await self.engine.dispose()
        await self.vector.close()


def build_runtime(settings: Settings, engine: AsyncEngine) -> Runtime:
    embedder = build_embedder(settings)
    vector = VectorStore(settings)
    llm = LLMProvider(settings)
    search = WebSearchProvider(settings)
    repo = Repository(engine, settings, embedder, vector)
    runner = CapabilityRunner(repo, search)
    agent = Agent(settings, repo, llm, runner)
    worker = Worker(repo, agent)
    mailbox = Mailbox()
    return Runtime(
        settings=settings,
        engine=engine,
        embedder=embedder,
        vector=vector,
        llm=llm,
        search=search,
        repo=repo,
        runner=runner,
        agent=agent,
        worker=worker,
        mailbox=mailbox,
    )
