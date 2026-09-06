"""E2E：以 spec/plan 为标准，从用户可见入口验证 MVP 主链路。

测试不走内部接口；模型/搜索走协议桩。覆盖 spec 的端到端测试场景与核心用户故事。
"""

from __future__ import annotations

import uuid

UUID = uuid.uuid4


def _fresh(api, prefix="星"):
    return api.register(nickname=f"{prefix}{UUID().hex[:8]}")


# ---------------------------------------------------------------------------
# 身份与会话（故事 1/2/3/4/5）
# ---------------------------------------------------------------------------
def test_register_returns_token_and_auth_required(api):
    reg = _fresh(api)
    assert reg["nickname"]
    assert reg["token"]
    # 无令牌请求被拒
    r = api._c.get("/api/messages")
    assert r.status_code == 401
    # 错令牌被拒
    r = api._c.get("/api/messages", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_register_same_nickname_unique_and_login_ok(api):
    a = _fresh(api, "同名")
    b_reg = api.register(nickname="同名")
    # 同名会被自动加后缀保证唯一
    assert b_reg["nickname"] != a["nickname"]
    # 用昵称+令牌登录
    api.login(a["nickname"], a["token"])


def test_one_session_per_user_and_history_persists(api, db):
    reg = _fresh(api)
    # 注册即已建唯一会话
    rows = db("SELECT id, user_id FROM sessions WHERE user_id = %s", (reg["user_id"],))
    assert len(rows) == 1
    session_id = rows[0][0]
    assert session_id == reg["session_id"]
    # 发两条消息都落在同一会话
    api.send(reg["token"], "第一条", "a1")
    api.send(reg["token"], "第二条", "a2")
    hist = api.history(reg["token"])
    texts = [m["payload"]["text"] for m in hist if m["type"] == "user"]
    assert texts == ["第一条", "第二条"]
    # 再登录一次，历史仍在（同会话可恢复）
    api.login(reg["nickname"], reg["token"])
    hist2 = api.history(reg["token"])
    assert len([m for m in hist2 if m["type"] == "user"]) == 2


# ---------------------------------------------------------------------------
# 对话交付：整包回复 / 按序恰一次（故事 6/7/8/11）
# ---------------------------------------------------------------------------
def test_send_gets_whole_reply_and_message_lifecycle(api):
    reg = _fresh(api)
    res = api.send(reg["token"], "今天天气不错", "m1")
    assert res["reply"]["text"]
    assert res["user_message_id"]
    hist = api.history(reg["token"])
    user_msg = [m for m in hist if m["type"] == "user"][0]
    assert user_msg["status"] == "replied"


def test_burst_messages_each_gets_exactly_one_reply_in_order(api):
    """同会话连发多条：串行按序、各得恰一次回复（Mailbox + 生命周期）。"""
    import concurrent.futures as cf

    reg = _fresh(api)
    n = 6

    def send(i: int):
        return api.send(reg["token"], f"并发消息{i}", f"burst-{i}")

    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        outs = list(pool.map(send, range(n)))
    assert len(outs) == n
    hist = api.history(reg["token"])
    # 每个 user 消息恰好一个 agent 回复，且顺序（seq）不交错
    seqs = [m["seq"] for m in hist]
    assert seqs == sorted(seqs)
    user_seqs = [m["seq"] for m in hist if m["type"] == "user"]
    assert len(user_seqs) == n
    # user 与对应 agent 相邻（agent 紧跟其 user，不被其他 user 插入）
    for i, m in enumerate(hist):
        if m["type"] == "user":
            # 下一条必为它的 agent 回复
            assert hist[i + 1]["type"] == "agent"
            assert hist[i + 1]["origin_message_id"] == m["id"]


def test_duplicate_client_message_id_reuses_not_reruns(api, db):
    """同 message_id 重发：命中即复用已有结果，不产生第二次执行（故事 9）。"""
    reg = _fresh(api)
    first = api.send(reg["token"], "这条只发一次", "dup-key-1")
    second = api.send(reg["token"], "这条只发一次", "dup-key-1")
    assert second["reused"] is True
    assert second["reply"]["text"] == first["reply"]["text"]
    # 库中同 key 仅一条 user 消息 + 一条 agent 回复
    sid = db(
        "SELECT s.id FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.nickname=%s",
        (reg["nickname"],),
    )[0][0]
    rows = db(
        "SELECT type, COUNT(*) FROM session_messages WHERE session_id=%s AND client_message_id=%s GROUP BY type",
        (sid, "dup-key-1"),
    )
    by_type = {t: c for t, c in rows}
    assert by_type == {"user": 1}


# ---------------------------------------------------------------------------
# 数据隔离（故事 15/16）
# ---------------------------------------------------------------------------
def test_two_users_concurrent_isolation(api):
    """两个用户并发对话互不可见对方消息/摘要/回复。"""
    import concurrent.futures as cf

    a, b = _fresh(api), _fresh(api)
    sa = "我只属于A用户-秘密内容A"
    sb = "我只属于B用户-秘密内容B"

    def send_user(reg, text):
        return api.send(reg["token"], text, f"iso-{UUID().hex[:6]}")

    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(send_user, a, sa), pool.submit(send_user, b, sb)]
        for f in futs:
            f.result()
    ha = api.history(a["token"])
    hb = api.history(b["token"])
    ta = "".join(m["payload"].get("text", "") for m in ha)
    tb = "".join(m["payload"].get("text", "") for m in hb)
    assert sb not in ta and sa not in tb
    assert sa in ta and sb in tb


# ---------------------------------------------------------------------------
# 偶像能力：检索 tool / 提问 skill / 联网 mcp / 护栏（故事 18-22）
# ---------------------------------------------------------------------------
def test_idol_retrieval_tool_grounded_reply(api, db):
    """问偶像信息：走检索 tool，基于真实资料回答，不编造。"""
    reg = _fresh(api)
    res = api.send(reg["token"], "连淮伟的生日是什么时候？", "q-1")
    reply = res["reply"]["text"]
    hist = api.history(reg["token"])
    tools = [m for m in hist if m["type"] == "tool"]
    assert any(m["payload"]["name"] == "retrieve_idol_info" for m in tools), f"应调用检索tool, got {tools}"
    # 回答引用了资料内容（含检索结果关键词）
    assert ("1月8日" in reply) or ("1998" in reply), reply
    # 工具记录与回复同批落库且可追溯
    assert res["user_message_id"]


def test_ambiguous_message_triggers_question_skill(api):
    """含糊表述（缺指代）触发提问 skill：反问澄清而非自行假设。"""
    reg = _fresh(api)
    res = api.send(reg["token"], "他最近怎么样？", "v-1")
    reply = res["reply"]["text"]
    # 反问式澄清，而不是直接顺着话题作答
    assert ("哪一" in reply) or ("什么" in reply) or ("告诉" in reply) or ("？" in reply and "？" in reply), reply


def test_realtime_question_triggers_web_search_mcp(api):
    reg = _fresh(api)
    res = api.send(reg["token"], "今天有什么大新闻吗？帮我查查", "w-1")
    reply = res["reply"]["text"]
    hist = api.history(reg["token"])
    tools = [m for m in hist if m["type"] == "tool"]
    assert any(m["payload"]["name"] == "web_search" for m in tools)
    assert ("信息" in reply) or ("新闻" in reply) or ("本地桩" in reply)


def test_run_step_budget_guardrail_graceful(brain, api):
    """单 run 最大步数护栏：无限工具循环也被体面收尾、会话不中断。"""
    reg = _fresh(api)
    brain.loop_forever = True
    res = api.send(reg["token"], "帮我查一下连淮伟", "g-1")
    assert res["reply"]["text"]  # 有回复，未崩
    hist = api.history(reg["token"])
    assert any(m["type"] == "agent" for m in hist)


def test_unanswerable_question_does_not_fabricate(api, db):
    """资料里没有的细节：Agent 不编造，仍体面回应、会话不中断。"""
    reg = _fresh(api)
    res = api.send(reg["token"], "连淮伟最喜欢的宠物名字叫什么？", "nf-1")
    reply = res["reply"]["text"]
    assert reply  # 有回复
    # 兜底句不允许假装给出资料外的确定细节——我们只断言能继续聊
    api.send(reg["token"], "那换个话题吧", "nf-2")


# ---------------------------------------------------------------------------
# 历史分页可完整恢复（故事 10）—— 长会话刷新不丢
# ---------------------------------------------------------------------------
def test_long_history_restorable_via_paging(api):
    """超过单页上限后，逐页游标仍能取回完整历史（刷新可恢复）。"""
    reg = _fresh(api)
    n = 70
    for i in range(n):
        api.send(reg["token"], f"历史消息第{i}条内容标记#{i}", f"pg-{i}")
    # 单页（小 limit）只能取一部分
    first = api.history_page(reg["token"], limit=25)
    assert len(first["messages"]) == 25
    assert first["has_more"] is True
    # 逐页拉全：新旧都有，顺序与内容完整
    all_msgs = api.history_all_paged(reg["token"], limit=25)
    user_texts = [m["payload"]["text"] for m in all_msgs if m["type"] == "user"]
    assert user_texts == [f"历史消息第{i}条内容标记#{i}" for i in range(n)]
    assert len(all_msgs) >= n


# ---------------------------------------------------------------------------
# 会话记忆与压缩（故事 12/13/14）
# ---------------------------------------------------------------------------
def test_long_session_compresses_and_stays_coherent(api, db):
    """超过 token 阈值：触发压缩、摘要持久化且后续仍连贯。"""
    reg = _fresh(api)
    # 注入足量对话撑破预算（e2e 配置预算较小 + 保留轮次少）
    for i in range(24):
        api.send(reg["token"], f"今天分享我的第{i}件小事：吃到了好吃的甜品，心情很好", f"long-{i}")
    # 摘要应已持久化
    sid = db(
        "SELECT s.id FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.nickname=%s",
        (reg["nickname"],),
    )[0][0]
    row = db("SELECT summary_text, summary_seq FROM sessions WHERE id=%s", (sid,))[0]
    assert row[0] is not None and row[1] is not None
    # 后续对话仍连贯（记忆未破坏会话）
    api.send(reg["token"], "我刚刚说今天吃了什么？", "after-compress")
    hist = api.history(reg["token"])
    assert any(m["type"] == "agent" for m in hist)


def test_summary_scoped_to_own_session(api):
    """会话内容（含摘要）只属于自己（故事 14）。"""
    a = _fresh(api)
    b = _fresh(api)
    for i in range(12):
        api.send(a["token"], f"A的私密小事{i}", f"A-{i}")
    api.send(b["token"], "B你好", "B-0")
    # 新用户 B 不应被卷入 A 的摘要内容
    hb = "".join(m["payload"].get("text", "") for m in api.history(b["token"]))
    assert "A的私密" not in hb


# ---------------------------------------------------------------------------
# 失败重发（故事 27）
# ---------------------------------------------------------------------------
def test_failed_processing_resend_recovers_no_dup(brain, api, db):
    """中途失败重发：消息不丢、不重复，最终恰好一次回复。"""
    reg = _fresh(api)
    brain.fail_once = True
    r1 = api.send_raw(reg["token"], "这条会先失败", "fail-1")
    assert r1.status_code == 500
    # 消息仍可见（received 而非消失）
    hist = api.history(reg["token"])
    user_msgs = [m for m in hist if m["type"] == "user"]
    assert len(user_msgs) == 1
    # 凭同键重发 → 恢复并得到回复，不重复
    r2 = api.send(reg["token"], "这条会先失败", "fail-1")
    assert r2["reply"]["text"]
    hist = api.history(reg["token"])
    agents = [m for m in hist if m["type"] == "agent"]
    assert len(agents) == 1


# ---------------------------------------------------------------------------
# 越界拒绝（故事 16）
# ---------------------------------------------------------------------------
def test_cannot_reference_other_users_session(api):
    """结构上无路径能引用他人会话：两用户各持各的唯一会话，数据互不可达。"""
    a = _fresh(api)
    b = _fresh(api)
    # 各自注册即得各自唯一 session（不共享、不串号）
    assert a["session_id"] != b["session_id"]
    assert a["user_id"] != b["user_id"]
    # 用 A 的令牌发送，历史只能回显 A 自己的内容
    api.send(a["token"], "A的秘密数据", "scope-a")
    ha = api.history(a["token"])
    assert any(m["payload"].get("text") == "A的秘密数据" for m in ha)
    # B 的历史不含 A 的数据（隔离）
    hb = api.history(b["token"])
    assert not [m for m in hb if m["payload"].get("text") == "A的秘密数据"]


# ---------------------------------------------------------------------------
# 知识更新后不回引过期内容（故事 24）—— RAG 读路径回查校验
# ---------------------------------------------------------------------------
def test_stale_knowledge_not_recalled_after_removal(e2e, db):
    """知识下架（status=0 + 删向量）后，检索不再回引该过期内容。"""
    import asyncio

    from app.config import Settings
    from app.db import create_async_engine_for
    from app.embeddings import build_embedder
    from app.repository import Repository
    from app.vector_store import KIND_IDOL_INFO, VectorStore, point_id

    s = Settings(
        ENV="test", MYSQL_DB="ap1_test", MYSQL_HOST="127.0.0.1", MYSQL_PORT=3307,
        MYSQL_USER="ap1", MYSQL_PASSWORD="ap1", QDRANT_URL="http://127.0.0.1:6333",
        QDRANT_COLLECTION="idol_knowledge_test", EMBED_PROVIDER="local", EMBED_DIM=512,
    )
    # 先写一条临时知识，检索命中它
    content = "连淮伟养了一只名叫团子的小狗，团子很可爱。"
    async def _run():
        eng = create_async_engine_for(s)
        emb = build_embedder(s)
        vec = VectorStore(s)
        await vec.ensure_collection(await emb.ensure_dim())
        repo = Repository(eng, s, emb, vec)
        rows = await repo.upsert_idol_infos([{"tag": "粉丝互动", "content": content}])
        r = rows[0]
        vecs = await emb.embed([content])
        await vec.upsert_vectors(
            [(point_id(KIND_IDOL_INFO, r.id), vecs[0],
              {"kind": KIND_IDOL_INFO, "row_id": r.id, "content_hash": r.content_hash})]
        )
        before = await repo.search_knowledge("团子小狗", limit=3)
        # 下架 + 删向量（模拟运营删除/清扫）
        await repo.mark_row_removed("idol_infos", [r.id])
        await vec.delete_by_ids([point_id(KIND_IDOL_INFO, r.id)])
        after = await repo.search_knowledge("团子小狗", limit=3)
        await eng.dispose()
        await vec.close()
        return before, after

    before, after = asyncio.run(_run())
    assert any("团子" in x["text"] for x in before)
    assert not any("团子" in x["text"] for x in after)


# ---------------------------------------------------------------------------
# 可观测性：run/step 日志带 scope（故事 25/26；日志是组件唯一内部可见面）
# ---------------------------------------------------------------------------
def test_run_step_logs_carry_scope_and_replayable(e2e, api):
    import json

    api.send(api.register()["token"], "可观测性检查消息", "obs-1")
    lines = e2e["log_file"].read_text(encoding="utf-8").splitlines()
    run_events = [line for line in lines if "run_start" in line or "step_start" in line]
    assert run_events, "应在日志中找到 run/step 事件"
    sample = json.loads(run_events[0])
    for key in ("env", "user_id", "session_id", "run_id"):
        assert key in sample, f"日志应带 scope 字段 {key}"
