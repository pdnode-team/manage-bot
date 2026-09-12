import zulip
import time
import os
import re
from datetime import datetime
from functools import lru_cache
from dotenv import load_dotenv
from manager import ModManager
from typing import Any, Dict, Optional, cast

load_dotenv()

# ─── 1. Init ───

client = zulip.Client(config_file="zuliprc")
mgr = ModManager()
BOT_EMAIL = "manage-bot@chat.p67.click"
MUTE_CONFIRM_THRESHOLD = 7200  # 2 hours in seconds

# Notification channel (channel id 32 = #Bot)
NOTIFICATION_STREAM_ID = 32
NOTIFICATION_TOPIC = "manage-reminders"

# ─── 2. Helpers ───

@lru_cache(maxsize=128)
def get_user_info(name_or_id: str | int) -> tuple:
    """Look up user info. Returns: (role, user_id, full_name, email)"""
    users = client.get_users()
    if users.get("result") == "success":
        for m in users["members"]:
            if m["full_name"] == name_or_id or str(m["user_id"]) == str(name_or_id):
                return m["role"], m["user_id"], m["full_name"], m.get("email", "")
    return 400, -1, "Unknown", ""


def send_dm(user_id: int, content: str):
    """Send a direct message to a user."""
    client.send_message({"type": "private", "to": [user_id], "content": content})


def send_reminder(user_full_name: str, user_id: int, content: str) -> Dict[str, Any]:
    """
    Send a reminder to a specific user through the #Bot channel.
    The message is posted in stream id 32 under NOTIFICATION_TOPIC and mentions
    the user by full name (Zulip native mention via @**full_name**).
    All user-visible text is English.
    """
    request = {
        "type": "stream",
        "to": NOTIFICATION_STREAM_ID,
        "topic": NOTIFICATION_TOPIC,
        "content": f"@**{user_full_name}** {content}",
    }
    return client.send_message(request)


def send_custom(
    msg: Optional[Dict[str, Any]],
    content: str,
    to: Optional[str | int | list[int]] = None,
    topic: Optional[str] = None,
):
    """Smart message sender (same stream/topic or to moderators)."""
    m_type = msg.get("type", "stream") if msg else "stream"

    if not to:
        if msg:
            to = msg["display_recipient"] if m_type == "stream" else [msg["sender_id"]]
        else:
            to = "moderators"

    if m_type == "stream" and not topic:
        topic = msg.get("subject", "notification") if msg else "notification"

    request: Dict[str, Any] = {"type": m_type, "to": to, "content": content}
    if m_type == "stream":
        request["topic"] = topic

    return client.send_message(request)


# ─── 3. AI Moderation Engine removed (2026-08-15) ───

# ─── 4. Command Handlers ───

def cmd_help(msg, content):
    parts = content.split()
    if len(parts) > 1:
        detail = parts[1].lstrip("/")
        help_detail = {
            "mute": (
                "**/mute** -- Mute a user\n"
                "Usage: `/mute @user <duration>`\n"
                "Duration: `30m`, `1h`, `2h`, `1d`, `always`\n"
                "Note: Mutes over 2h require another moderator to confirm via `/confirm-mute @user`\n"
                "Example: `/mute @**User** 1h`\n"
                "Permission: Moderator+"
            ),
            "unmute": (
                "**/unmute** -- Unmute a user\n"
                "Usage: `/unmute @user`\n"
                "Note: If the mute was set by someone else, another moderator must confirm via `/confirm-unmute @user`\n"
                "Example: `/unmute @**User**`\n"
                "Permission: Moderator+"
            ),
            "warn": (
                "**/warn** -- Warn a user (auto-escalates mute)\n"
                "Usage: `/warn @user <rule_id> [reason]`\n"
                "Rule IDs: `1.1` Disrespectful, `1.2` Bad Words, `1.3` Mentions Abuse, `1.4` Spamming,\n"
                "  `2.1` School Policy, `2.2` Impersonation, `2.3` Privacy, `3.2` System Abuse\n"
                "Example: `/warn @**User** 1.2 swearing`\n"
                "Permission: Moderator+"
            ),
            "unwarn": (
                "**/unwarn** -- Remove the last warn for a rule\n"
                "Usage: `/unwarn @user <rule_id>`\n"
                "Note: If the warn was issued by someone else, another moderator must confirm via `/confirm-unwarn @user <rule_id>`\n"
                "Example: `/unwarn @**User** 1.2`\n"
                "Permission: Moderator+"
            ),
            "status": (
                "**/status** -- View user's warn stats\n"
                "Usage: `/status @user` -- warn counts per rule\n"
                "`/status all` -- list all currently muted users"
            ),
            "userinfo": (
                "**/userinfo** -- View user details\n"
                "Usage: `/userinfo @user`\n"
                "Shows: ID, role, mute status, warn stats"
            ),
            "logs": (
                "**/logs** -- View moderation history\n"
                "Usage: `/logs @user` -- history for a user\n"
                "`/logs recent` -- recent actions (last 20)\n"
                "Permission: Moderator+"
            ),
            "purge": (
                "**/purge** -- Bulk delete a user's recent messages\n"
                "Usage: `/purge @user [count]`\n"
                "Default 10, max 100\n"
                "Example: `/purge @**User** 20`\n"
                "Permission: Moderator+"
            ),
            "stats": (
                "**/stats** -- View a user's message stats\n"
                "Usage: `/stats @user`\n"
                "Shows: last 100 messages broken down by channel"
            ),
            "clear-cache": (
                "**/clear-cache** -- Clear internal caches\n"
                "Permission: Admin+"
            ),
            "confirm-mute": (
                "**/confirm-mute** -- Confirm a pending mute (>2h)\n"
                "Usage: `/confirm-mute @user`\n"
                "Permission: Moderator+ (must be different from requester)"
            ),
            "confirm-unmute": (
                "**/confirm-unmute** -- Confirm a pending unmute\n"
                "Usage: `/confirm-unmute @user`\n"
                "Permission: Moderator+ (must be different from requester)"
            ),
            "confirm-unwarn": (
                "**/confirm-unwarn** -- Confirm a pending unwarn\n"
                "Usage: `/confirm-unwarn @user <rule_id>`\n"
                "Example: `/confirm-unwarn @**User** 1.2`\n"
                "Permission: Moderator+ (must be different from requester)"
            ),
            "create-rule": (
                "**/create-rule** -- Create an auto-moderation rule\n"
                "Usage: `/create-rule <target> <scope> \"<pattern>\" <action> [\"name\"]`\n"
                "Target: `user:@**Name**` or `role:moderator` (moderators & below)\n"
                "Scope: `stream:#name`, `stream:#name:topic1,topic2`, `topic:name`, `everywhere`\n"
                "Action: `allow` (skip), `delete`, `warn` (DM user), `mutewarn` (silent)\n"
                "Example: `/create-rule role:moderator stream:#general \"badword\" delete \"Bad word filter\"`\n"
                "Permission: Admin only"
            ),
            "update-rule": (
                "**/update-rule** -- Update an auto-rule field\n"
                "Usage: `/update-rule <id> <field> <value>`\n"
                "Fields: `pattern`, `action`, `target`, `scope`, `enabled` (0/1), `name`\n"
                "Example: `/update-rule 5 action delete`\n"
                "Permission: Admin only"
            ),
            "delete-rule": (
                "**/delete-rule** -- Delete an auto-rule\n"
                "Usage: `/delete-rule <id>`\n"
                "Permission: Admin only"
            ),
            "check-rule": (
                "**/check-rule** -- View auto-rule details\n"
                "Usage: `/check-rule <id>`\n"
                "Permission: Admin only"
            ),
            "list-rules": (
                "**/list-rules** -- List all auto-rules\n"
                "Usage: `/list-rules`\n"
                "Permission: Admin only"
            ),
        }
        info = help_detail.get(detail, f"Unknown command: /{detail}")
        send_custom(msg, info)
        return

    send_custom(
        msg,
        "📋 **Manage Bot Commands**\n\n"
        "**🔇 Moderation** (Moderator+)\n"
        "* `/mute @user <duration>` -- Mute a user\n"
        "* `/unmute @user` -- Unmute a user\n"
        "* `/warn @user <rule_id> [reason]` -- Warn a user\n"
        "* `/unwarn @user <rule_id>` -- Remove last warn\n\n"
        "**✅ Confirmations** (Moderator+, different from requester)\n"
        "* `/confirm-mute @user` -- Confirm mute >2h\n"
        "* `/confirm-unmute @user` -- Confirm unmute\n"
        "* `/confirm-unwarn @user <rule_id>` -- Confirm unwarn\n\n"
        "**👤 Info**\n"
        "* `/status @user` -- Warn stats\n"
        "* `/status all` -- Mute list\n"
        "* `/userinfo @user` -- User details\n"
        "* `/logs @user` -- Mod history\n"
        "* `/logs recent` -- Recent actions\n"
        "* `/stats @user` -- Message stats\n\n"
        "**🗑️ Management** (Moderator+)\n"
        "* `/purge @user [count]` -- Bulk delete messages\n\n"
        "**⚙️ System**\n"
        "* `/clear-cache` -- Clear caches\n"
        "* `/help [command]` -- This help\n\n"
        "**🔒 New Rules Lockdown** (Admin only)\n"
        "* `/new-rules-need-allow` -- Lockdown: bots only, everyone must `/agree-new-rules`\n"
        "* `/agree-new-rules` -- Accept new rules & regain send ability\n\n"
        "**🤖 Auto-Rules** (Admin only)\n"
        "* `/create-rule <target> <scope> \"<pattern>\" <action> [\"name\"]` -- Create\n"
        "* `/update-rule <id> <field> <value>` -- Update\n"
        "* `/delete-rule <id>` -- Delete\n"
        "* `/check-rule <id>` -- View details\n"
        "* `/list-rules` -- List all\n"
        "Target: `user:@**Name**` or `role:moderator`\n"
        "Scope: `stream:#name`, `stream:#name:topic1,topic2`, `topic:name`, `everywhere`\n"
        "Action: `allow`, `delete`, `warn`, `mutewarn`",
    )


def cmd_mute(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Mute a user. If >2h, requires confirmation from another moderator."""
    match = re.search(r"@\*\*(.*?)(?:\|\d+)?\*\*", content)
    time_str = content[match.end():].strip() if match else content.split("/mute", 1)[-1].strip()
    secs, label = mgr.parse_time(time_str)

    if secs is None:
        send_custom(msg, "❌ Invalid format. Use `1h`, `30m` or `always`.")
        return

    # Check if this duration needs confirmation (>2h or permanent)
    # Only moderators (role=300) need confirmation; admins (200) and owner (100) bypass
    needs_confirm = ((secs == -1) or (secs > MUTE_CONFIRM_THRESHOLD)) and s_role == 300

    if needs_confirm:
        # Create pending confirmation
        pending_id = mgr.create_pending(
            action_type="mute",
            actor_id=sender_id,
            actor_name=sender_name,
            target_id=t_id,
            target_name=t_name,
            duration=label,
            duration_seconds=secs,
        )
        send_custom(
            msg,
            f"⏳ @**{t_name}** mute ({label}) exceeds 2h limit. "
            f"Another moderator must confirm with `/confirm-mute @**{t_name}**`.",
        )
        # Post to moderators channel
        pendings = mgr.list_pending()
        pending_list = "".join(
            f"  • #{p['id']} **{p['action_type']}** @**{p['target_name']}** "
            f"by @**{p['actor_name']}**"
            + (f" ({p['duration']})" if p['duration'] else "")
            + "\n"
            for p in pendings
        )
        send_custom(
            None,
            f"⏳ **Pending {label} mute**: @**{t_name}** by @**{sender_name}**\n"
            f"Use `/confirm-mute @**{t_name}**` to approve.\n"
            + (f"\n**All pending:**\n{pending_list}" if len(pendings) > 1 else ""),
            "moderators", "Pending Confirmations",
        )
        return

    # Execute directly for <=2h
    mgr.set_mute(t_id, secs)
    mgr.log_action(sender_id, sender_name, "mute", t_id, t_name, f"Muted {label}")
    send_custom(msg, f"✅ @**{t_name}** muted for {label}.")
    send_dm(t_id, f"🔇 You have been muted for **{label}** by a moderator.")
    send_custom(
        None,
        f"🔇 **Mute**: @**{t_name}** by @**{sender_name}** ({label})",
        "moderators", "Manual Mutes",
    )


def cmd_confirm_mute(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Confirm a pending mute."""
    pending = mgr.get_pending(t_id, "mute")
    if not pending:
        send_custom(
            msg,
            f"❌ No pending mute confirmation for @**{t_name}**. "
            f"Either it was already handled or the request has expired.",
        )
        return

    # Check confirmer is not the original requester
    if pending.actor_id == sender_id:
        send_custom(msg, f"❌ You cannot confirm your own mute request. Another moderator must do it.")
        return

    # Execute the mute
    secs = pending.duration_seconds if pending.duration_seconds else -1
    mgr.set_mute(t_id, secs)
    mgr.log_action(
        pending.actor_id, pending.actor_name, "mute",
        t_id, t_name,
        f"Muted {pending.duration or 'forever'} (confirmed by @**{sender_name}**)",
    )
    mgr.remove_pending(pending.id)

    send_custom(
        msg,
        f"✅ Mute confirmed. @**{t_name}** muted for {pending.duration or 'forever'} "
        f"(requested by @**{pending.actor_name}**, confirmed by @**{sender_name}**).",
    )
    send_dm(t_id, f"🔇 You have been muted for **{pending.duration or 'forever'}** by a moderator.")
    send_custom(
        None,
        f"🔇 **Mute Executed**: @**{t_name}** for {pending.duration or 'forever'}\n"
        f"Requested by: @**{pending.actor_name}** | Confirmed by: @**{sender_name}**",
        "moderators", "Manual Mutes",
    )


def cmd_unmute(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Unmute a user. If original muter was someone else, needs confirmation."""
    # Check who originally muted
    original = mgr.get_last_mute_actor(t_id)

    # If the mute was set by someone else (not themselves), need confirmation
    # Only moderators (role=300) need confirmation; admins (200) and owner (100) bypass
    if original and original["id"] != sender_id and s_role == 300:
        pending_id = mgr.create_pending(
            action_type="unmute",
            actor_id=sender_id,
            actor_name=sender_name,
            target_id=t_id,
            target_name=t_name,
            original_actor_id=original["id"],
            original_actor_name=original["name"],
        )
        send_custom(
            msg,
            f"⏳ @**{t_name}** was muted by @**{original['name']}**, not you. "
            f"Another moderator must confirm with `/confirm-unmute @**{t_name}**`.",
        )
        send_custom(
            None,
            f"⏳ **Pending unmute**: @**{t_name}** by @**{sender_name}**\n"
            f"Originally muted by: @**{original['name']}**\n"
            f"Use `/confirm-unmute @**{t_name}**` to approve.",
            "moderators", "Pending Confirmations",
        )
        return

    # Execute directly (same person who muted, or no record)
    if mgr.unmute(t_id):
        mgr.log_action(sender_id, sender_name, "unmute", t_id, t_name, "Unmuted")
        send_custom(msg, f"✅ @**{t_name}** unmuted.")
        send_dm(t_id, "🔊 You have been unmuted.")
        send_custom(
            None,
            f"🔊 **Unmute**: @**{t_name}** by @**{sender_name}**",
            "moderators", "Manual Mutes",
        )
    else:
        send_custom(msg, f"❌ @**{t_name}** is not currently muted.")


def cmd_confirm_unmute(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Confirm a pending unmute."""
    pending = mgr.get_pending(t_id, "unmute")
    if not pending:
        send_custom(
            msg,
            f"❌ No pending unmute confirmation for @**{t_name}**.",
        )
        return

    if pending.actor_id == sender_id:
        send_custom(msg, f"❌ You cannot confirm your own unmute request.")
        return

    if mgr.unmute(t_id):
        mgr.log_action(
            pending.actor_id, pending.actor_name, "unmute",
            t_id, t_name,
            f"Unmuted (confirmed by @**{sender_name}**)",
        )
        mgr.remove_pending(pending.id)
        send_custom(
            msg,
            f"✅ Unmute confirmed. @**{t_name}** unmuted "
            f"(requested by @**{pending.actor_name}**, confirmed by @**{sender_name}**).",
        )
        send_dm(t_id, "🔊 You have been unmuted.")
        send_custom(
            None,
            f"🔊 **Unmute Executed**: @**{t_name}**\n"
            f"Requested by: @**{pending.actor_name}** | Confirmed by: @**{sender_name}**",
            "moderators", "Manual Mutes",
        )
    else:
        send_custom(msg, f"❌ @**{t_name}** is not currently muted.")
        mgr.remove_pending(pending.id)


def cmd_warn(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Warn a user (escalates mute by rule formula). No confirmation needed for warns."""
    match = re.search(r"@\*\*(.*?)(?:\|\d+)?\*\*", content)
    remaining = content[match.end():].strip() if match else ""
    sub_parts = remaining.split()
    if not sub_parts:
        send_custom(msg, "❌ Format: `/warn @user <rule_id> [reason]`")
        return

    rid = sub_parts[0]
    reason = " ".join(sub_parts[1:]) if len(sub_parts) > 1 else "No reason"
    res, err = mgr.warn_user(t_id, rid, reason, actor_id=sender_id, actor_name=sender_name)
    if err:
        send_custom(msg, f"❌ {err}")
    else:
        name = (res or {}).get("name") or "unknown"
        count = (res or {}).get("count") or 0
        mute_mins = (res or {}).get("mute_mins") or 0
        txt = f"⚠ @**{t_name}** warned (Rule {rid}: {name})\nCounts: {count} | Mute: {mute_mins}m"
        send_custom(msg, txt)
        mgr.log_action(sender_id, sender_name, "warn", t_id, t_name, f"Rule {rid} ({name}) - {reason}")
        send_dm(t_id, f"⚠️ You have received a warning (Rule {rid}: {name}).\nReason: {reason}")
        send_custom(
            None,
            f"{txt}\nAdmin: @**{sender_name}**\nReason: {reason}",
            "moderators", "Manual Action",
        )


def cmd_unwarn(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Remove the last warn for a rule. If original warner was someone else, needs confirmation."""
    parts = content.split()
    rid = parts[-1]

    # Check who issued the last warn for this rule
    original = mgr.get_last_warn_actor(t_id, rid)

    # Only moderators (role=300) need confirmation; admins (200) and owner (100) bypass
    if original and original["id"] != sender_id and s_role == 300:
        pending_id = mgr.create_pending(
            action_type="unwarn",
            actor_id=sender_id,
            actor_name=sender_name,
            target_id=t_id,
            target_name=t_name,
            rule_id=rid,
            original_actor_id=original["id"],
            original_actor_name=original["name"],
        )
        send_custom(
            msg,
            f"⏳ @**{t_name}**'s warn (Rule {rid}) was issued by @**{original['name']}**, not you. "
            f"Another moderator must confirm with `/confirm-unwarn @**{t_name}** {rid}`.",
        )
        send_custom(
            None,
            f"⏳ **Pending unwarn**: @**{t_name}** Rule {rid} by @**{sender_name}**\n"
            f"Originally warned by: @**{original['name']}**\n"
            f"Use `/confirm-unwarn @**{t_name}** {rid}` to approve.",
            "moderators", "Pending Confirmations",
        )
        return

    # Execute directly
    res, err = mgr.unwarn_user(t_id, rid)
    if err:
        send_custom(msg, f"❌ {err}")
    else:
        name = (res or {}).get("name") or "unknown"
        count = (res or {}).get("count") or 0
        txt = f"♻️ @**{t_name}** unwarned (Rule {rid}: {name})\nCounts: {count}"
        send_custom(msg, txt)
        mgr.log_action(sender_id, sender_name, "unwarn", t_id, t_name, f"Unwarned {rid}")
        send_dm(t_id, f"♻️ Your warning (Rule {rid}: {name}) has been removed.")
        send_custom(
            None,
            f"{txt}\nAdmin: @**{sender_name}**",
            "moderators", "Manual Action",
        )


def cmd_confirm_unwarn(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    """Confirm a pending unwarn."""
    # Parse rule_id from the message
    parts = content.split()
    rid = parts[-1] if len(parts) > 1 else None
    if not rid:
        send_custom(msg, "❌ Format: `/confirm-unwarn @user <rule_id>`")
        return

    pending = mgr.get_pending(t_id, "unwarn")
    if not pending:
        send_custom(
            msg,
            f"❌ No pending unwarn confirmation for @**{t_name}** (Rule {rid}).",
        )
        return

    if pending.actor_id == sender_id:
        send_custom(msg, f"❌ You cannot confirm your own unwarn request.")
        return

    if pending.rule_id != rid:
        send_custom(
            msg,
            f"❌ Rule mismatch. Pending unwarn is for Rule {pending.rule_id}, "
            f"not Rule {rid}.",
        )
        return

    res, err = mgr.unwarn_user(t_id, rid)
    if err:
        send_custom(msg, f"❌ {err}")
    else:
        name = (res or {}).get("name") or "unknown"
        count = (res or {}).get("count") or 0
        txt = f"♻️ @**{t_name}** unwarned (Rule {rid}: {name}) -- confirmed\nCounts: {count}"
        send_custom(
            msg,
            f"✅ Unwarn confirmed. @**{t_name}** unwarned (Rule {rid})\n"
            f"Requested by: @**{pending.actor_name}** | Confirmed by: @**{sender_name}**",
        )
        mgr.log_action(
            pending.actor_id, pending.actor_name, "unwarn",
            t_id, t_name,
            f"Unwarned {rid} (confirmed by @**{sender_name}**)",
        )
        send_dm(t_id, f"♻️ Your warning (Rule {rid}) has been removed (confirmed by moderator).")
        send_custom(
            None,
            f"{txt}\nAdmin: @**{sender_name}**",
            "moderators", "Manual Action",
        )

    mgr.remove_pending(pending.id)


def cmd_status(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    data = mgr.get_user_status(t_id)
    status_txt = (
        ", ".join([f"**{k}**: {v}" for k, v in data.items()])
        if data
        else "No warns"
    )
    send_custom(msg, f"📊 @**{t_name}**: {status_txt}")


def cmd_status_all(msg):
    active_mutes = mgr.get_all_mutes()
    m_list = [
        f"- @**{get_user_info(u)[2]}**: {'Forever' if e == -1.0 else f'{int((e - time.time()) / 60)}m left'}"
        for u, e in active_mutes.items()
    ]
    # Show pending confirmations too
    pending_list = mgr.list_pending()
    p_text = ""
    if pending_list:
        p_items = [
            f"  • **{p['action_type']}** @**{p['target_name']}** "
            f"by @**{p['actor_name']}**"
            + (f" ({p['duration']})" if p['duration'] else "")
            + (f" Rule {p['rule_id']}" if p['rule_id'] else "")
            for p in pending_list
        ]
        p_text = "\n⏳ **Pending Confirmations:**\n" + "\n".join(p_items)

    send_custom(
        msg,
        "🔇 **Current Mutes:**\n" + ("\n".join(m_list) if m_list else "None") + p_text,
    )


def cmd_clear_cache(msg, content):
    sender_id = msg.get("sender_id")
    s_role, _, _, _ = get_user_info(sender_id)
    if s_role > 300:
        return
    get_user_info.cache_clear()
    send_custom(msg, "✅ All caches have been cleared.")


def cmd_new_rules_need_allow(msg, content):
    """Lockdown: only users who /agree-new-rules can send messages."""
    sender_id = msg.get("sender_id")
    sender_name = msg.get("sender_full_name")
    s_role, _, _, _ = get_user_info(sender_id)
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can use this command.")
        return
    if mgr.is_new_rules_active():
        send_custom(
            msg,
            "⚠️ Lockdown is **already active**. Send `/end-new-rules` to lift it, or wait for users to `/agree-new-rules`.",
        )
        return
    mgr.activate_new_rules(sender_id, sender_name or f"user_{sender_id}")
    send_custom(
        msg,
        "📜 **New rules lockdown activated!**\n"
        "All non-bot users must type `/agree-new-rules` to regain the ability to send messages.\n"
        "This applies to everyone, including admins.",
    )
    send_custom(
        None,
        f"🔒 **New Rules Lockdown**: Activated by @**{sender_name}**\n"
        "All users (including admins) must `/agree-new-rules` before they can send messages.",
        "moderators", "New Rules",
    )
    mgr.log_action(sender_id, sender_name, "lockdown", 0, "system", "New rules lockdown activated")


def cmd_end_new_rules(msg, content):
    """Lift the new-rules lockdown. Admin only."""
    sender_id = msg.get("sender_id")
    sender_name = msg.get("sender_full_name")
    s_role, _, _, _ = get_user_info(sender_id)
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can use this command.")
        return
    if not mgr.is_new_rules_active():
        send_custom(msg, "ℹ️ Lockdown is not currently active.")
        return
    mgr.deactivate_new_rules()
    send_custom(
        msg,
        "🔓 **New rules lockdown lifted.** All users can send messages without agreeing.",
    )
    send_custom(
        None,
        f"🔓 **New Rules Lockdown**: Lifted by @**{sender_name}**",
        "moderators", "New Rules",
    )
    mgr.log_action(sender_id, sender_name, "end_lockdown", 0, "system", "New rules lockdown lifted")


def cmd_agree_new_rules(msg, content):
    """User agrees to new rules, regaining ability to send messages."""
    sender_id = msg.get("sender_id")
    sender_name = msg.get("sender_full_name")
    if mgr.has_agreed_new_rules(sender_id):
        send_custom(msg, "✅ You've already agreed to the new rules. You're all set!")
        return
    mgr.agree_new_rules(sender_id)
    send_custom(msg, f"✅ Thanks, @**{sender_name}**! You've agreed to the new rules. You may now send messages.")
    mgr.log_action(sender_id, sender_name, "agree_rules", sender_id, sender_name, "Agreed to new rules")


def cmd_userinfo(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    t_role, _, _, _ = get_user_info(t_id)

    muted, mute_until_ts = mgr.is_muted(t_id)
    if muted:
        if mute_until_ts == -1.0:
            mute_status = "🔇 Permanent mute"
        else:
            remaining = int((mute_until_ts - time.time()) / 60)
            mute_status = f"🔇 Muted ({remaining}m remaining)"
    else:
        mute_status = "✅ Normal"

    role_names = {100: "Owner 👑", 200: "Admin 🛡️", 300: "Moderator ⚔️", 400: "Member 👤", 600: "Guest 🚪"}
    role_name = role_names.get(t_role, f"Unknown ({t_role})")

    warn_stats = mgr.get_user_status(t_id)
    warn_str = ", ".join([f"{k}: {v}" for k, v in warn_stats.items()]) if warn_stats else "None"

    send_custom(
        msg,
        f"👤 **{t_name}**\nID: `{t_id}`\nRole: {role_name}\nMute: {mute_status}\nWarns: {warn_str}",
    )


def cmd_logs_recent(msg):
    logs = mgr.get_recent_logs(20)
    if not logs:
        send_custom(msg, "📋 No recent actions.")
        return
    action_icons = {"mute": "🔇", "unmute": "🔊", "warn": "⚠️", "unwarn": "♻️", "purge": "🗑️"}
    lines = []
    for log in logs:
        ts = log["timestamp"].strftime("%m-%d %H:%M")
        icon = action_icons.get(log["action"], "📌")
        lines.append(f"  {ts} {icon} {log['actor_name']} -> @**{log['target_name']}** -- {log['details']}")
    send_custom(msg, "📋 **Recent Actions:**\n" + "\n".join(lines[:15]))


def cmd_logs(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    if s_role > 300:
        return
    logs = mgr.get_user_logs(t_id, 20)
    if not logs:
        send_custom(msg, f"📋 @**{t_name}**: No history.")
        return
    action_icons = {"mute": "🔇", "unmute": "🔊", "warn": "⚠️", "unwarn": "♻️", "purge": "🗑️"}
    lines = []
    for log in logs:
        ts = log["timestamp"].strftime("%m-%d %H:%M")
        icon = action_icons.get(log["action"], "📌")
        lines.append(f"  {ts} {icon} by {log['actor_name']} -- {log['details']}")
    send_custom(msg, f"📋 @**{t_name}** History:\n" + "\n".join(lines[:15]))


def cmd_purge(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    parts = content.split()
    count = 10
    for part in parts:
        if part.isdigit():
            count = int(part)
            break
    count = min(max(count, 1), 100)

    result = client.get_messages({
        "anchor": "newest",
        "num_before": count,
        "num_after": 0,
        "narrow": [["sender", t_email]],
        "apply_markdown": False,
    })

    if result.get("result") != "success":
        send_custom(msg, f"❌ Failed to fetch messages: {result.get('msg', 'Unknown error')}")
        return

    msgs = result.get("messages", [])
    if not msgs:
        send_custom(msg, f"ℹ️ @**{t_name}** has no recent messages.")
        return

    deleted = 0
    errors = 0
    for m in msgs[:count]:
        try:
            client.delete_message(m["id"])
            deleted += 1
        except Exception as e:
            errors += 1
            print(f"Purge delete error (msg {m['id']}): {e}")

    result_msg = f"🗑️ Purged {deleted} messages from @**{t_name}**"
    if errors:
        result_msg += f" ({errors} failed)"
    send_custom(msg, result_msg)

    mgr.log_action(sender_id, sender_name, "purge", t_id, t_name, f"Deleted {deleted} messages")
    send_custom(
        None,
        f"🗑️ **Purge**: @**{t_name}** by @**{sender_name}** ({deleted} msgs)",
        "moderators", "Manual Action",
    )


def cmd_stats(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email):
    result = client.get_messages({
        "anchor": "newest",
        "num_before": 100,
        "num_after": 0,
        "narrow": [["sender", t_email]],
        "apply_markdown": False,
    })

    if result.get("result") != "success":
        send_custom(msg, f"❌ Failed: {result.get('msg', 'Unknown error')}")
        return

    msgs = result.get("messages", [])
    total = len(msgs)
    if total == 0:
        send_custom(msg, f"📈 @**{t_name}**: No recent messages found.")
        return

    streams: dict[str, int] = {}
    for m in msgs:
        if m.get("type") == "stream":
            stream = m.get("display_recipient", "?")
        else:
            stream = "DM / PM"
        streams[stream] = streams.get(stream, 0) + 1

    latest_ts = max(m.get("timestamp", 0) for m in msgs)
    latest_str = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M")

    sorted_streams = sorted(streams.items(), key=lambda x: -x[1])[:10]
    stream_lines = [f"  #{s}: {c}" for s, c in sorted_streams]

    send_custom(
        msg,
        f"📈 **@{t_name}** Stats (last 100 messages)\n"
        f"Total: {total}\n"
        f"Latest: {latest_str}\n"
        f"Channels:\n" + "\n".join(stream_lines),
    )


# ─── 4.5 Auto-Rule Commands (Admin only: role <= 200) ───

def cmd_create_rule(msg, content, s_role, sender_id):
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can manage auto-rules.")
        return
    # Format: /create-rule <target> <scope> "<pattern>" <action> ["name"]
    # target: user:@**Name** | role:moderator
    # scope: stream:#name | topic:name | stream:#name:topic1,topic2 | everywhere
    # pattern: regex in double quotes
    # action: allow | delete | warn | mutewarn
    # name: optional quoted name at end
    import re as re_mod, json as json_mod
    rest = content.split(None, 1)[1] if len(content.split(None, 1)) > 1 else ""
    if not rest:
        send_custom(msg, "❌ Usage: `/create-rule <target> <scope> \"<pattern>\" <action> [\"name\"]`\n"
                    "Examples:\n"
                    "`/create-rule user:@**User** stream:#general \"badword.*\" delete`\n"
                    "`/create-rule role:moderator topic:offtopic \"spam\" warn \"Spam in offtopic\"`\n"
                    "`/create-rule role:moderator stream:#general:topic1,topic2 \"regex\" delete`")
        return

    # Parse target
    target_match = re_mod.match(r"(user:@\*\*(.+?)\*\*|role:moderator)\s+", rest)
    if not target_match:
        send_custom(msg, "❌ Invalid target. Use `user:@**Name**` or `role:moderator`.")
        return
    target_raw = target_match.group(1)
    rest = rest[target_match.end():]

    if target_raw.startswith("user:"):
        target_type = "user"
        # Extract user name from @**Name**
        full_name = target_match.group(2)
        _, target_id, _, _ = get_user_info(full_name)
        if target_id == -1:
            send_custom(msg, f"❌ User `{full_name}` not found.")
            return
        target_value = str(target_id)
    else:
        target_type = "role"
        target_value = "moderator"

    # Parse scope
    scope_match = re_mod.match(
        r"(stream:#(.+?)(?::([\w,\-]+))?|topic:([\w\-]+)|everywhere)\s+", rest
    )
    if not scope_match:
        send_custom(msg, "❌ Invalid scope. Use `stream:#name`, `stream:#name:topic1,topic2`, `topic:name`, or `everywhere`.")
        return
    scope_raw = scope_match.group(1)
    rest = rest[scope_match.end():]

    scope_stream_id = None
    scope_topics = None
    if scope_raw == "everywhere":
        pass  # both None = match all
    elif scope_raw.startswith("stream:"):
        stream_name = scope_match.group(2)
        result = client.get_stream_id(stream_name)
        if result.get("result") != "success":
            send_custom(msg, f"❌ Stream `#{stream_name}` not found.")
            return
        scope_stream_id = result["stream_id"]
        # Optional topic list after stream name
        topic_str = scope_match.group(3)
        if topic_str:
            scope_topics = json_mod.dumps([t.strip() for t in topic_str.split(",")])
    elif scope_raw.startswith("topic:"):
        topic_name = scope_match.group(4)
        # topic scope without stream: match across all streams
        scope_topics = json_mod.dumps([topic_name])

    # Parse quoted pattern
    pattern_match = re_mod.match(r'"((?:[^"\\]|\\.)*)"\s+', rest)
    if not pattern_match:
        send_custom(msg, "❌ Pattern must be in double quotes. Example: `\"badword.*\"`")
        return
    pattern = pattern_match.group(1)
    rest = rest[pattern_match.end():]

    # Validate regex
    try:
        re_mod.compile(pattern)
    except re_mod.error as e:
        send_custom(msg, f"❌ Invalid regex: {e}")
        return

    # Parse action
    action_match = re_mod.match(r"(allow|delete|warn|mutewarn)\s*", rest)
    if not action_match:
        send_custom(msg, "❌ Invalid action. Use `allow`, `delete`, `warn`, or `mutewarn`.")
        return
    action = action_match.group(1)
    rest = rest[action_match.end():]

    # Parse optional name
    name = None
    name_match = re_mod.match(r'"((?:[^"\\]|\\.)*)"\s*', rest)
    if name_match:
        name = name_match.group(1)

    rule_id = mgr.create_auto_rule(
        name=name, target_type=target_type, target_value=target_value,
        scope_stream_id=scope_stream_id, scope_topics=scope_topics,
        pattern=pattern, action=action, created_by=sender_id,
    )

    # Build human-readable summary
    target_label = f"user id={target_value}" if target_type == "user" else "moderators & below"
    scope_label = "everywhere"
    if scope_stream_id is not None:
        streams_res = client.get_streams()
        sname = "?"
        if streams_res.get("result") == "success":
            for s in streams_res["streams"]:
                if s["stream_id"] == scope_stream_id:
                    sname = s["name"]
                    break
        if scope_topics:
            scope_label = f"stream:#{sname} topics:{','.join(json_mod.loads(scope_topics))}"
        else:
            scope_label = f"stream:#{sname} (all topics)"
    elif scope_topics:
        scope_label = f"topic:{','.join(json_mod.loads(scope_topics))} (any stream)"

    send_custom(
        msg,
        f"✅ **Auto-Rule #{rule_id} created**\n"
        f"Target: `{target_label}`\n"
        f"Scope: `{scope_label}`\n"
        f"Pattern: `{pattern}`\n"
        f"Action: `{action}`\n"
        + (f"Name: `{name}`\n" if name else "")
    )
    # Notify moderators
    name_tag = f" ({name})" if name else ""
    send_custom(
        None,
        f"📋 **New Auto-Rule #{rule_id}{name_tag}**\n"
        f"Created by: @**{get_user_info(sender_id)[2]}**\n"
        f"Target: `{target_label}` | Scope: `{scope_label}`\n"
        f"Pattern: `{pattern}` | Action: `{action}`",
        "moderators", "Auto-Rules",
    )


def cmd_update_rule(msg, content, s_role, sender_id):
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can manage auto-rules.")
        return
    import re as re_mod
    parts = content.split()
    if len(parts) < 4:
        send_custom(msg, "❌ Usage: `/update-rule <id> <field> <value>`")
        return
    try:
        rule_id = int(parts[1])
    except ValueError:
        send_custom(msg, "❌ Rule ID must be a number.")
        return
    field = parts[2]
    value = " ".join(parts[3:])

    rule = mgr.get_auto_rule(rule_id)
    if not rule:
        send_custom(msg, f"❌ Rule #{rule_id} not found.")
        return

    kwargs = {}
    if field == "pattern":
        # Strip surrounding quotes if present
        pvalue = value
        if len(pvalue) >= 2 and pvalue.startswith('"') and pvalue.endswith('"'):
            pvalue = pvalue[1:-1]
        elif len(pvalue) >= 2 and pvalue.startswith("'") and pvalue.endswith("'"):
            pvalue = pvalue[1:-1]
        try:
            re_mod.compile(pvalue)
        except re_mod.error as e:
            send_custom(msg, f"❌ Invalid regex: {e}")
            return
        kwargs["pattern"] = pvalue
    elif field == "action":
        if value not in ("allow", "delete", "warn", "mutewarn"):
            send_custom(msg, "❌ Action must be: allow, delete, warn, mutewarn")
            return
        kwargs["action"] = value
    elif field == "target":
        if value.startswith("user:"):
            uname = value[5:]
            _, uid, _, _ = get_user_info(uname)
            if uid == -1:
                send_custom(msg, f"❌ User `{uname}` not found.")
                return
            kwargs["target_type"] = "user"
            kwargs["target_value"] = str(uid)
        elif value == "role:moderator":
            kwargs["target_type"] = "role"
            kwargs["target_value"] = "moderator"
        else:
            send_custom(msg, "❌ Target must be `user:Name` or `role:moderator`.")
            return
    elif field == "scope":
        # Re-parse scope just like create
        sm = re_mod.match(
            r"(stream:#(.+?)(?::([\w,\-]+))?|topic:([\w\-]+)|everywhere)", value
        )
        if not sm:
            send_custom(msg, "❌ Invalid scope. See `/create-rule` for syntax.")
            return
        if sm.group(1) == "everywhere":
            kwargs["scope_stream_id"] = None
            kwargs["scope_topics"] = None
        elif sm.group(1).startswith("stream:"):
            sname = sm.group(2)
            result = client.get_stream_id(sname)
            if result.get("result") != "success":
                send_custom(msg, f"❌ Stream `#{sname}` not found.")
                return
            kwargs["scope_stream_id"] = result["stream_id"]
            tstr = sm.group(3)
            if tstr:
                kwargs["scope_topics"] = json.dumps([t.strip() for t in tstr.split(",")])
            else:
                kwargs["scope_topics"] = None
        elif sm.group(1).startswith("topic:"):
            kwargs["scope_stream_id"] = None
            kwargs["scope_topics"] = json.dumps([sm.group(4)])
    elif field == "enabled":
        if value.lower() in ("1", "true", "yes"):
            kwargs["enabled"] = 1
        elif value.lower() in ("0", "false", "no"):
            kwargs["enabled"] = 0
        else:
            send_custom(msg, "❌ enabled must be 0 or 1.")
            return
    elif field == "name":
        kwargs["name"] = value if value.lower() != "none" else None
    else:
        send_custom(msg, "❌ Unknown field. Valid: pattern, action, target, scope, enabled, name")
        return

    if mgr.update_auto_rule(rule_id, **kwargs):
        send_custom(msg, f"✅ Rule #{rule_id} updated (`{field}`).")
    else:
        send_custom(msg, f"❌ Rule #{rule_id} not found.")


def cmd_delete_rule(msg, content, s_role, sender_id):
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can manage auto-rules.")
        return
    parts = content.split()
    if len(parts) < 2:
        send_custom(msg, "❌ Usage: `/delete-rule <id>`")
        return
    try:
        rule_id = int(parts[1])
    except ValueError:
        send_custom(msg, "❌ Rule ID must be a number.")
        return
    rule = mgr.get_auto_rule(rule_id)
    if not rule:
        send_custom(msg, f"❌ Rule #{rule_id} not found.")
        return
    mgr.delete_auto_rule(rule_id)
    send_custom(msg, f"🗑️ Rule #{rule_id} deleted (`{rule.get('action','?')}` on `{rule.get('pattern','?')}`).")
    send_custom(
        None,
        f"🗑️ **Auto-Rule #{rule_id} deleted** by @**{get_user_info(sender_id)[2]}**\n"
        f"Was: `{rule.get('action','?')}` on `{rule.get('pattern','?')}`",
        "moderators", "Auto-Rules",
    )


def cmd_check_rule(msg, content, s_role):
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can view auto-rules.")
        return
    parts = content.split()
    if len(parts) < 2:
        send_custom(msg, "❌ Usage: `/check-rule <id>`")
        return
    try:
        rule_id = int(parts[1])
    except ValueError:
        send_custom(msg, "❌ Rule ID must be a number.")
        return
    rule = mgr.get_auto_rule(rule_id)
    if not rule:
        send_custom(msg, f"❌ Rule #{rule_id} not found.")
        return

    target_label = f"user id={rule['target_value']}" if rule["target_type"] == "user" else "moderators & below (role>=300)"
    scope_label = "everywhere"
    if rule["scope_stream_id"] is not None:
        sname = "?"
        streams_res = client.get_streams()
        if streams_res.get("result") == "success":
            for s in streams_res["streams"]:
                if s["stream_id"] == rule["scope_stream_id"]:
                    sname = s["name"]
                    break
        if rule["scope_topics"]:
            import json
            scope_label = f"stream:#{sname} topics:{','.join(json.loads(rule['scope_topics']))}"
        else:
            scope_label = f"stream:#{sname} (all topics)"
    elif rule["scope_topics"]:
        import json
        scope_label = f"topic:{','.join(json.loads(rule['scope_topics']))} (any stream)"

    send_custom(
        msg,
        f"🔍 **Auto-Rule #{rule['id']}**\n"
        + (f"Name: `{rule['name']}`\n" if rule["name"] else "")
        + f"Target: `{target_label}`\n"
        f"Scope: `{scope_label}`\n"
        f"Pattern: `{rule['pattern']}`\n"
        f"Action: `{rule['action']}`\n"
        f"Enabled: `{'Yes' if rule['enabled'] else 'No'}`\n"
        f"Created by: `{rule['created_by']}` at `{rule['created_at']}`"
    )


def cmd_list_rules(msg, s_role):
    if s_role > 200:
        send_custom(msg, "⛔ Only admins can view auto-rules.")
        return
    rules = mgr.list_auto_rules()
    if not rules:
        send_custom(msg, "📋 No auto-rules configured.")
        return

    lines = []
    for r in rules:
        e = "✅" if r["enabled"] else "⏸️"
        t = f"user:{r['target_value']}" if r["target_type"] == "user" else "mods&below"
        n = f" `{r['name']}`" if r["name"] else ""
        lines.append(f"  #{r['id']}{n} {e} {t} → `{r['action']}` /`{r['pattern'][:40]}/`")
    send_custom(msg, "📋 **Auto-Rules:**\n" + "\n".join(lines))


# ─── 5. Command Routing Table ───

NO_MENTION_COMMANDS = {
    "/help": cmd_help,
    "/clear-cache": cmd_clear_cache,
    "/new-rules-need-allow": cmd_new_rules_need_allow,
    "/agree-new-rules": cmd_agree_new_rules,
    "/agree-with-rules": cmd_agree_new_rules,
    "/end-new-rules": cmd_end_new_rules,
    # auto-rule commands handled inline in handle_message
}

MENTION_COMMANDS: dict[str, Any] = {
    "/mute": cmd_mute,
    "/unmute": cmd_unmute,
    "/warn": cmd_warn,
    "/unwarn": cmd_unwarn,
    "/status": cmd_status,
    "/userinfo": cmd_userinfo,
    "/logs": cmd_logs,
    "/purge": cmd_purge,
    "/stats": cmd_stats,
    "/confirm-mute": cmd_confirm_mute,
    "/confirm-unmute": cmd_confirm_unmute,
    "/confirm-unwarn": cmd_confirm_unwarn,
}

PERMISSION_CMDS = {"/mute", "/unmute", "/warn", "/unwarn", "/purge"}
CONFIRM_CMDS = {"/confirm-mute", "/confirm-unmute", "/confirm-unwarn"}
AUTO_RULE_CMDS = {"/create-rule", "/update-rule", "/delete-rule", "/check-rule", "/list-rules"}


# ─── 6. Message Handler ───

def handle_message(msg: Dict[str, Any]):
    if msg.get("sender_email") == BOT_EMAIL:
        return

    sender_id: int = cast(int, msg.get("sender_id"))
    sender_name = msg.get("sender_full_name")
    content: str = msg.get("content", "").strip()

    # A. Mute check (Global: applies to all streams and DMs)
    muted, _ = mgr.is_muted(sender_id)
    if muted:
        s_role, _, _, _ = get_user_info(sender_id)
        if s_role != 100:
            client.delete_message(msg["id"])
            return
    else:
        s_role, _, _, _ = get_user_info(sender_id)

    # A5. New rules lockdown check (Global: applies to all streams and DMs)
    if mgr.is_new_rules_active():
        if not mgr.has_agreed_new_rules(sender_id):
            cmd = content.split()[0].lower() if content else ""
            # Allow /agree-new-rules, /agree-with-rules, and admin commands to pass through
            if (
                cmd not in ("/agree-new-rules", "/agree-with-rules", "/new-rules-need-allow", "/end-new-rules")
                and sender_id not in mgr.bot_ids
            ):
                client.delete_message(msg["id"])
                # Send DM to inform user (rate-limited: 60s between DMs)
                if mgr.can_send_lockdown_dm(sender_id):
                    send_dm(
                        sender_id,
                        "🔒 **New rules have been updated!**\n\n"
                        "Please type `/agree-new-rules` (or `/agree-with-rules`) in DM or the #Bot channel to accept the new rules and regain the ability to send messages.\n"
                        "This applies to all users, including admins.\n\n"
                        "Thank you for your cooperation!",
                    )
                return

    # C. Auto-Rule check (non-command messages, Global: applies to all streams and DMs)
    if not content.startswith("/"):
        stream_id = msg.get("stream_id")
        topic = msg.get("subject", "")
        auto_match = mgr.match_auto_rules(
            user_id=sender_id, role=s_role,
            stream_id=stream_id, topic=topic, content=content,
        )
        if auto_match:
            action = auto_match["action"]
            rule_id = auto_match["id"]
            rule_label = f"Auto-Rule #{rule_id}"
            if auto_match.get("name"):
                rule_label += f" ({auto_match['name']})"

            if action == "allow":
                # Allow: message passes through, skip further auto-rules
                pass

            elif action in ("delete", "warn", "mutewarn"):
                # Delete the message
                try:
                    client.delete_message(msg["id"])
                except Exception as e:
                    print(f"Auto-rule delete error: {e}")

                # Build notification for moderators
                msg_link = ""
                if msg.get("type") == "stream" and msg.get("stream_id"):
                    sid = msg["stream_id"]
                    st = msg.get("subject", "")
                    msg_link = f"https://chat.p67.click/#narrow/channel/{sid}/topic/{st}/near/{msg['id']}"

                action_icon = {"delete": "🗑️", "warn": "⚠️", "mutewarn": "🔇"}.get(action, "📌")
                mod_alert = (
                    f"{action_icon} **{rule_label}** triggered\n"
                    f"User: @**{sender_name}**\n"
                    f"Action: `{action}`\n"
                    f"Pattern: `{auto_match['pattern']}`\n"
                    f"Match:\n```quote\n{content[:300]}\n```\n"
                )
                if msg_link:
                    mod_alert += f"[Message]({msg_link})\n"
                if action == "warn":
                    mod_alert += f"Use `/warn @**{sender_name}** <rule_id> [reason]` to issue a warning.\n"
                elif action == "mutewarn":
                    mod_alert += f"This is a silent warn (no DM to user). Use `/warn @**{sender_name}** <rule_id>` if needed.\n"
                elif action == "delete":
                    mod_alert += "Message was automatically deleted."

                send_custom(None, mod_alert.strip(), "moderators", "Auto-Rules")
                return  # Message already deleted, don't process further

    # B. Command handling (Restricted to DMs only — bot only replies there)
    if msg.get("type") != "private":
        return

    if content.startswith("/"):
        cmd = content.split()[0].lower()

        # B0. Auto-rule commands (admin only, no @mention needed)
        if cmd in AUTO_RULE_CMDS:
            if cmd == "/create-rule":
                cmd_create_rule(msg, content, s_role, sender_id)
            elif cmd == "/update-rule":
                cmd_update_rule(msg, content, s_role, sender_id)
            elif cmd == "/delete-rule":
                cmd_delete_rule(msg, content, s_role, sender_id)
            elif cmd == "/check-rule":
                cmd_check_rule(msg, content, s_role)
            elif cmd == "/list-rules":
                cmd_list_rules(msg, s_role)
            return

        # B1. Commands without @mention
        if cmd == "/status" and "all" in content.lower().split():
            cmd_status_all(msg)
            return
        if cmd == "/logs" and "recent" in content.lower().split():
            if s_role > 300:
                return
            cmd_logs_recent(msg)
            return

        no_mention = NO_MENTION_COMMANDS.get(cmd)
        if no_mention:
            no_mention(msg, content)
            return

        # B2. Parse @mention
        match = re.search(r"@\*\*(.*?)(?:\|\d+)?\*\*", content)
        if not match:
            return
        t_name = match.group(1).strip()
        t_role, t_id, _, t_email = get_user_info(t_name)
        if t_id == -1:
            return

        # B3. Permission check for moderation commands
        if cmd in PERMISSION_CMDS:
            if s_role > 300:
                send_custom(msg, "⛔ You don't have permission for this command.")
                return
            if s_role >= t_role and not (s_role == 100 and cmd == "/unmute"):
                send_custom(
                    msg,
                    f"⛔ Hierarchy Error: Access denied for @**{t_name}**.",
                )
                return

        # B4. Permission check for confirm commands
        if cmd in CONFIRM_CMDS:
            if s_role > 300:
                send_custom(msg, "⛔ You don't have permission for this command.")
                return

        # B5. Dispatch to handler
        handler = MENTION_COMMANDS.get(cmd)
        if handler:
            handler(msg, sender_id, sender_name, content, s_role, t_name, t_id, t_email)
        return

    # D. AI moderation removed (2026-08-15)


# ─── 7. Entry Point ───

if __name__ == "__main__":
    # Run DB migration first
    from models import migrate_db
    migrate_db()

    # Fetch bot IDs for new-rules lockdown
    try:
        users = client.get_users()
        if users.get("result") == "success":
            bot_ids = [m["user_id"] for m in users["members"] if m.get("is_bot")]
            mgr.set_bot_ids(bot_ids)
            print(f"Loaded {len(bot_ids)} bot IDs for new-rules lockdown: {bot_ids}")
    except Exception as e:
        print(f"Warning: Could not fetch bot IDs: {e}")

    try:
        print("🛡 Pdnode Manage Bot v9.0 (auto-rules + DM notifications) started...")
        client.call_on_each_message(handle_message)
    except KeyboardInterrupt:
        print("\nBye bye.")
