from typing import Any
from astrbot.api.event import filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "astrbot_plugin_portrayal",
    "Zhalslar",
    "分析群友的性格画像",
    "1.0.3",
)
class Relationship(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 上下文缓存
        self.contexts_cache: dict[str, list[dict[str, str]]] = {}

    def _build_user_context(
        self, round_messages: list[dict[str, Any]], target_id: str
    ) -> list[dict[str, str]]:
        """
        把指定用户在所有回合里的纯文本消息打包成 openai-style 的 user 上下文。
        """

        contexts: list[dict[str, str]] = []

        for msg in round_messages:
            # 1. 过滤发送者
            if msg["sender"]["user_id"] != int(target_id):
                continue

            # 2. 提取并拼接所有 text 片段
            text_segments = [
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
            ]
            text = "".join(text_segments).strip()
            # 3. 仅当真正说了话才保留
            if text:
                print(text)
                contexts.append({"role": "user", "content": text})

        return contexts

    async def get_msg_contexts(
        self, event: AiocqhttpMessageEvent, target_id: str, max_query_rounds: int
    ) -> tuple[list[dict], int]:
        """持续获取群聊历史消息直到达到要求"""
        group_id = event.get_group_id()
        query_rounds = 0
        message_seq = 0
        contexts: list[dict] = []
        while len(contexts) < self.conf["max_msg_count"]:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": self.conf["per_msg_count"],
                "reverseOrder": True,
            }
            result: dict = await event.bot.api.call_action(
                "get_group_msg_history", **payloads
            )
            round_messages = result["messages"]
            if not round_messages:
                break
            message_seq = round_messages[0]["message_id"]

            contexts.extend(self._build_user_context(round_messages, target_id))
            query_rounds += 1
            if query_rounds >= max_query_rounds:
                break
        return contexts, query_rounds

    async def get_llm_respond(
        self, nickname: str, gender: str, contexts: list[dict]
    ) -> str | None:
        """调用llm回复"""
        get_using = self.context.get_using_provider()
        if not get_using:
            return None
        try:
            system_prompt = self.conf["system_prompt_template"].format(
                nickname=nickname, gender=("他" if gender == "male" else "她")
            )
            llm_response = await get_using.text_chat(
                system_prompt=system_prompt,
                prompt=f"这是 {nickname} 的聊天记录",
                contexts=contexts,
            )
            return llm_response.completion_text

        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            return None

    async def get_nickname(
        self, event: AiocqhttpMessageEvent, user_id: str | int
    ) -> tuple[str, str]:
        """获取指定群友的昵称和性别"""
        all_info = await event.bot.get_group_member_info(
            group_id=int(event.get_group_id()), user_id=int(user_id)
        )
        nickname = all_info.get("card") or all_info.get("nickname")
        gender = all_info.get("sex")
        return nickname, gender

    async def get_at_id(self, event: AiocqhttpMessageEvent) -> str | None:
        return next(
            (
                str(seg.qq)
                for seg in event.get_messages()
                if (isinstance(seg, Comp.At)) and str(seg.qq) != event.get_self_id()
            ),
            None,
        )

    @filter.command("画像")
    async def get_portrayal(
        self,
        event: AiocqhttpMessageEvent,
        at_name: str | None = None,
        max_query_rounds: int | None = None,
    ):
        """
        画像 @群友 <查询轮数>
        """
        target_id: str = await self.get_at_id(event) or event.get_sender_id()
        nickname, gender = await self.get_nickname(event, target_id)
        contexts, query_rounds = None, None
        if self.contexts_cache and target_id in self.contexts_cache:
            contexts = self.contexts_cache[target_id]
        else:
            # 每轮查询200条消息，200轮查询4w条消息,几乎接近漫游极限
            target_query_rounds = min(
                200, max(0, max_query_rounds or int(self.conf["max_query_rounds"]))
            )
            yield event.plain_result(
                f"正在发起{target_query_rounds}轮查询来获取{nickname}的消息..."
            )
            contexts, query_rounds = await self.get_msg_contexts(
                event, target_id, target_query_rounds
            )
            self.contexts_cache[target_id] = contexts
        if not contexts:
            yield event.plain_result("没有找到该群友的任何消息")
            return

        if query_rounds:
            yield event.plain_result(
                f"已从{query_rounds * self.conf['per_msg_count']}条群消息中获取了{len(contexts)}条{nickname}的消息，正在分析..."
            )
        else:
            yield event.plain_result(
                f"已从缓存中获取了{len(contexts)}条{nickname}的消息，正在分析..."
            )

        try:
            llm_respond = await self.get_llm_respond(nickname, gender, contexts)
            if llm_respond:
                url = await self.text_to_image(llm_respond)
                yield event.image_result(url)
                del self.contexts_cache[target_id]
            else:
                yield event.plain_result("LLM响应为空")
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败:{e}")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.contexts_cache.clear()
