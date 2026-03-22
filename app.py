import zulip
import time
import os
import re
import json
import unicodedata
from openai import OpenAI
from functools import lru_cache
from dotenv import load_dotenv
from manager import ModManager
from typing import cast

load_dotenv()

# --- 1. 初始化 ---
aiClient = OpenAI()  # 默认读取 OPENAI_API_KEY
deepseekClient = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com"
)
client = zulip.Client(config_file="zuliprc")
mgr = ModManager()
BOT_EMAIL = "manage-bot@chat.p67.click"


# --- 2. 工具函数 ---
@lru_cache(maxsize=128)
def get_user_info(name_or_id):
    users = client.get_users()
    if users.get("result") == "success":
        for m in users["members"]:
            if m["full_name"] == name_or_id or str(m["user_id"]) == str(name_or_id):
                return m["role"], m["user_id"], m["full_name"]
    return 400, None, "Unknown"


def send_custom(msg, content, to=None, topic=None):
    m_type = msg["type"] if msg else "stream"
    if not to:
        to = msg["display_recipient"] if m_type == "stream" else msg["sender_id"]
    if m_type == "stream" and not topic:
        topic = msg.get("subject", "notification")

    request = {
        "type": m_type,
        "to": [to] if m_type == "private" else to,
        "content": content,
    }
    if m_type == "stream":
        request["topic"] = topic
    return client.send_message(request)


# --- 3. AI 审核引擎 ---
@lru_cache(maxsize=1024)
def check_openai(text):
    if len(text.strip()) < 2:
        return False, None
    norm = unicodedata.normalize("NFKC", text)
    clean = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", norm).lower()
    try:
        for t in [text, clean]:
            resp = aiClient.moderations.create(input=t, model="omni-moderation-latest")
            res = resp.results[0]
            if res.flagged:
                cat = next(
                    (k for k, v in res.categories.model_dump().items() if v),
                    "violation",
                )
                return True, cat
    except Exception as e:
        print(f"❌ OpenAI Error: {e}")
    return False, None


def check_deepseek(text):
    try:
        resp = deepseekClient.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Role: Neutral Community Moderator.\n"
                        "Rules: 1. Flag REAL abuse/porn/illegal acts. 2. IGNORE slang/hyperbole (e.g., '起义', '视奸', '闭嘴').\n"
                        "Guidelines: This is an open social group. Be lenient with jokes. Strictly ban explicit porn/real threats.\n"
                        "Output: JSON {'flagged': bool, 'reason': str, 'm': int}.\n"
                        "Constraint: Reason in English, <50 words. 'm' is mute duration in minutes.\n"
                        "Scope of bans: 1m - 10y, Please choose carefully!"
                    ),
                },
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(cast(str, resp.choices[0].message.content))
    except Exception as e:
        print(f"❌ DeepSeek Error: {e}")
        return {"flagged": False}


# --- 4. 消息处理 ---
def handle_message(msg):
    sender_id = msg.get("sender_id")
    sender_name = msg.get("sender_full_name")
    content = msg.get("content", "").strip()
    if msg.get("sender_email") == BOT_EMAIL:
        return

    # A. 禁言拦截
    muted, exp = mgr.is_muted(sender_id)
    if muted and sender_id != 8:
        client.delete_message(msg["id"])
        return

    # 设置通用变量
    s_role, _, _ = get_user_info(sender_id)

    # B. 命令处理
    if content.startswith("/"):
        match = re.search(r"@\*\*(.*?)(?:\|\d+)?\*\*", content)
        cmd = content.split()[0].lower()

        # 特殊：/status all 无需提及用户
        if cmd == "/status" and "all" in content:
            m_list = [
                f"- @**{get_user_info(u)[2]}**: {'Forever' if e == -1 else f'{int((e - time.time()) / 60)}m left'}"
                for u, e in mgr.mutes.items()
            ]
            send_custom(
                msg, "🔇 **Mutes:**\n" + ("\n".join(m_list) if m_list else "None")
            )
            return

        elif cmd == "/clear-cache" and not s_role > 300:
            get_user_info.cache_clear()  # 手动清空所有已缓存的角色信息
            check_openai.cache_clear()
            send_custom(msg, "All caches have been cleared.")
            return

        if not match:
            return

        t_name = match.group(1).strip()
        t_role, t_id, _ = get_user_info(t_name)

        # 权限检查
        if cmd in ["/mute", "/unmute", "/warn", "/unwarn"]:
            if s_role > 300:
                return
            if s_role >= t_role and not (s_role == 100 and cmd == "/unmute"):
                send_custom(
                    msg, f"⛔ Hierarchy Error: Access denied for @**{t_name}**."
                )
                return

        if cmd == "/mute":
            time_str = content[match.end() :].strip()
            secs, label = mgr.parse_time(time_str)
            if secs is not None:
                mgr.set_mute(t_id, secs)
                send_custom(msg, f"✅ @**{t_name}** muted for {label}.")
                send_custom(
                    None,
                    f"🚨 **Mute**: @**{t_name}** by @**{sender_name}** ({label})",
                    "moderators",
                    "Manual Mutes",
                )
            else:
                send_custom(msg, "❌ Invalid format. Use `1h`, `30m` or `always`.")

        elif cmd == "/unmute":
            if mgr.unmute(t_id):
                send_custom(msg, f"✅ @**{t_name}** unmuted.")

        elif cmd == "/warn":
            rid = content.split()[-1]
            res, err = mgr.warn_user(t_id, rid)
            if err:
                send_custom(msg, f"❌ {err}")
            else:
                name = (res or {}).get("name") or "unknown"
                count = (res or {}).get("count") or 0
                mute_mins = (res or {}).get("mute_mins") or 0
                txt = f"⚠ @**{t_name}** warned (Rule {rid}: {name})\nCounts: {count} | Mute: {mute_mins}m"
                send_custom(msg, txt)
                send_custom(
                    None,
                    f"{txt}\nAdmin: @**{sender_name}**",
                    "moderators",
                    "Manual Action",
                )

        elif cmd == "/unwarn":
            rid = content.split()[-1]
            res, err = mgr.unwarn_user(t_id, rid)
            if err:
                send_custom(msg, f"❌ {err}")
            else:
                name = (res or {}).get("name") or "unknown"
                count = (res or {}).get("count") or 0
                # mute_mins = (res or {}).get("mute_mins") or "unknown"
                txt = f"⚠ @**{t_name}** unwarned (Rule {rid}: {name})\nCounts: {count}"
                send_custom(msg, txt)
                send_custom(
                    None,
                    f"{txt}\nAdmin: @**{sender_name}**",
                    "moderators",
                    "Manual Action",
                )

        elif cmd == "/status":
            data = mgr.warns.get(str(t_id), {})
            status_txt = (
                ", ".join([f"R{k}: {v}" for k, v in data.items()])
                if data
                else "No warns"
            )
            send_custom(msg, f"📊 @**{t_name}**: {status_txt}")
        return

    if s_role <= 300:
        return

    # C. AI 审计 (雷达模式)
    flagged, reason = check_openai(content)
    if flagged:
        # 发送初步告警
        # stream_id = msg.get("stream_id", "")
        # link_topic = (
        #    msg.get("subject", "") if msg.get("subject") != "general chat" else ""
        # )
        # msg_link = (
        #    f"https://chat.p67.click/#narrow/channel/{stream_id}/topic/{link_topic}/near/{msg['id']}"
        #    if msg["type"] == "stream"
        #    else "PM"
        # )

        # alert = f"⚠ **AI Alert**\n**User**: @**{sender_name}**\n**Reason**: `{reason}`\n[Link]({msg_link})\n```quote\n{content}\n```"
        # send_custom(None, alert, "moderators", "Automatic detection")

        # temp_msg = send_custom(
        #    msg,
        #    f"@**{sender_name}** Your message appears to violate our rules and has been reported to the advanced AI team.",
        # )

        # DeepSeek 二次判定
        time.sleep(1)
        res = check_deepseek(content)
        if res.get("flagged"):
            client.delete_message(msg["id"])
            # if temp_msg.get("id"):
            #    client.delete_message(temp_msg["id"])

            m = res.get("m", 10)
            mgr.set_mute(sender_id, m * 60)
            final_txt = f"🚫 @**{sender_name}** Your comment has been **confirmed** to be in violation of our rules.\nPenalty: {m}-minute ban.\nReason: {res.get('reason')}"
            send_custom(msg, final_txt)
            send_custom(
                None,
                f"🚫 **Auto Mute**: @**{sender_name}** ({m}m)\nReason: {res.get('reason')}",
                "moderators",
                "Automatic mute",
            )
        # else:
        #    if temp_msg.get("id"):
        #        client.delete_message(temp_msg["id"])
        #    send_custom(
        #       msg,
        #       f"✅ @**{sender_name}** After verification, there was no problem with your statement.",
        #   )


if __name__ == "__main__":
    try:
        print("🛡 Pdnode Ultimate Bot v5.0 (Full Logic) started...")
        client.call_on_each_message(handle_message)
    except KeyboardInterrupt:
        print("\nBye bye.")

