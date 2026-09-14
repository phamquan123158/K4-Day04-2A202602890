from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

# Nạp các module cốt lõi từ starter_v0
from chat import now_iso, run_model_tool_loop, safe_slug, trim_history, write_transcript
from env_loader import load_lab_env
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"
TRANSCRIPTS_DIR = ROOT / "transcripts"
load_lab_env(ROOT)


def format_assistant_reply(raw_text: str) -> tuple[str, dict[str, Any] | None]:
    """Bóc tách câu trả lời tự nhiên từ trường reply của JSON schema."""
    if not raw_text:
        return "", None
    try:
        # Tìm khối JSON trong phản hồi
        cleaned = raw_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        data = json.loads(cleaned)
        if isinstance(data, dict) and "reply" in data:
            reply = data.get("reply", "")
            meta = {k: v for k, v in data.items() if k != "reply"}
            return reply, meta
    except Exception:
        pass
    return raw_text, None


# Cấu hình trang giao diện Streamlit
st.set_page_config(
    page_title="IT Helpdesk Agent — Live Chat",
    page_icon="🛠️",
    layout="wide",
)

st.title("🛠️ IT Helpdesk Agent — Live Chat")
st.caption("Giao diện tương tác trực tiếp với Helpdesk Agent có hiển thị minh bạch Tool Calling.")

# -------------------------------------------------------------
# 1. SIDEBAR: Quản lý Provider, Phiên bản và Thông tin Audit
# -------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Cấu hình Agent")

    provider_choice = st.selectbox(
        "Model Provider",
        options=["gemini", "openrouter", "openai", "anthropic"],
        index=0,
        help="Provider đã cấu hình API key trong file .env",
    )

    model_override = st.text_input(
        "Model Name (tùy chọn)",
        value="gemini-3.5-flash-lite",
        help="Model sử dụng. Mặc định là gemini-3.5-flash-lite để tránh cạn quota.",
    )

    version_choice = st.selectbox(
        "Artifact Version",
        options=["v0", "v1", "v2", "v3"],
        index=0,
        help="Chọn version prompt/tool tương ứng từ bạn A và B.",
    )

    system_prompt_path = ARTIFACTS_DIR / "system_prompt.md"
    tools_path = ARTIFACTS_DIR / "tools.yaml"

    # Tính toán artifact version và hash theo chuẩn của hệ thống
    try:
        artifact_ver = build_artifact_version(version_choice, system_prompt_path, tools_path)
        hashes = artifact_version_dict(artifact_ver)
        st.divider()
        st.subheader("🔍 Audit Information")
        st.code(
            f"Version: {artifact_ver.artifact_version}\n"
            f"Prompt Hash: {hashes['prompt_hash'][:12]}...\n"
            f"Tools Hash:  {hashes['tools_hash'][:12]}...",
            language="text",
        )
    except Exception as exc:
        st.error(f"Lỗi nạp artifact: {exc}")
        st.stop()

    st.divider()
    if st.button("🗑️ Làm mới cuộc hội thoại", use_container_width=True):
        st.session_state.messages = []
        st.session_state.chat_history = []
        st.session_state.turn_records = []
        st.session_state.session_id = f"session_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
        st.rerun()

# -------------------------------------------------------------
# 2. KHỞI TẠO STATE VÀ PHIÊN LÀM VIỆC
# -------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []  # Lưu turn cho agent loop
if "turn_records" not in st.session_state:
    st.session_state.turn_records = []
if "session_id" not in st.session_state:
    st.session_state.session_id = f"session_{datetime.now().strftime('%Y%m%dT%H%M%S')}"

# Chuẩn bị transcript path
transcript_file = TRANSCRIPTS_DIR / f"{safe_slug(version_choice)}_{safe_slug(st.session_state.session_id)}.json"

# -------------------------------------------------------------
# 3. HIỂN THỊ CÂU HỎI MẪU NẾU CHƯA CHAT GÌ
# -------------------------------------------------------------
if not st.session_state.messages:
    st.info(
        "💡 **Gợi ý câu hỏi thử nghiệm:**\n"
        "- *Kiểm tra trạng thái dịch vụ VPN trên môi trường production.*\n"
        "- *Kiểm tra chẩn đoán kết nối VPN trên máy tính LT-204.*\n"
        "- *Tra cứu thông tin nhân viên EMP-1007 xem được cấp thiết bị nào.*\n"
        "- *Kiểm tra giúp tôi chiếc laptop này.* (Thử nghiệm xem Agent có biết hỏi lại mã máy không)"
    )

# -------------------------------------------------------------
# 4. HIỂN THỊ LỊCH SỬ CHAT VÀ TOOL TRACES
# -------------------------------------------------------------
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        # Nếu là assistant và có tool events thì hiển thị expander chi tiết
        if msg["role"] == "assistant" and msg.get("tool_events"):
            events = msg["tool_events"]
            with st.expander(f"🛠️ Đã thực thi {len(events)} công cụ (Tool Call Trace)", expanded=False):
                for idx, ev in enumerate(events, 1):
                    st.markdown(f"**#{idx} Tool: `{ev.get('tool')}`**")
                    col_arg, col_res = st.columns(2)
                    with col_arg:
                        st.caption("Arguments truyền vào:")
                        st.json(ev.get("args", {}))
                    with col_res:
                        st.caption("Kết quả trả về từ hệ thống:")
                        st.json(ev.get("result", {}))
                    if idx < len(events):
                        st.divider()

        clean_text, meta_info = format_assistant_reply(msg["content"])
        st.markdown(clean_text)
        if meta_info:
            badges = []
            if "intent" in meta_info:
                badges.append(f"🎯 **Intent:** `{meta_info['intent']}`")
            if "action" in meta_info:
                badges.append(f"⚡ **Action:** `{meta_info['action']}`")
            if "evidence_ids" in meta_info:
                badges.append(f"📁 **Evidence:** `{meta_info['evidence_ids']}`")
            if badges:
                st.caption(" | ".join(badges))

# -------------------------------------------------------------
# 5. XỬ LÝ KHI NGƯỜI DÙNG NHẬP TIN NHẮN MỚI
# -------------------------------------------------------------
user_prompt = st.chat_input("Nhập yêu cầu hỗ trợ IT của bạn vào đây...")

if user_prompt:
    # 1. Hiển thị câu hỏi của user
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    # 2. Chuẩn bị chạy agent loop
    with st.chat_message("assistant"):
        with st.spinner("Đang xử lý và kiểm tra công cụ..."):
            try:
                system_prompt_text = system_prompt_path.read_text(encoding="utf-8")
                declarations = load_tool_declarations(tools_path)
                openai_tools = to_openai_tools(declarations)
                provider = make_provider(provider_choice)

                # Giữ cửa sổ ngữ cảnh lịch sử 5 lượt gần nhất
                recent_history = trim_history(st.session_state.chat_history, window=5)
                turn_msg = {"role": "user", "content": user_prompt}
                messages_for_model = [
                    {"role": "system", "content": system_prompt_text},
                    *recent_history,
                    turn_msg,
                ]

                # Chạy vòng lặp chuẩn của Helpdesk Agent
                outcome = run_model_tool_loop(
                    provider=provider,
                    messages=messages_for_model,
                    tools=openai_tools,
                    model=model_override.strip() or None,
                    max_tool_rounds=4,
                )

                ans_text = outcome.get("assistant_text", "")
                tool_events = outcome.get("tool_events", [])
                status = outcome.get("status", "answered")

                # Cập nhật lịch sử
                st.session_state.chat_history.append(turn_msg)
                st.session_state.chat_history.append({"role": "assistant", "content": ans_text})

                # Hiển thị tool trace nếu có
                if tool_events:
                    with st.expander(f"🛠️ Đã thực thi {len(tool_events)} công cụ (Tool Call Trace)", expanded=True):
                        for idx, ev in enumerate(tool_events, 1):
                            st.markdown(f"**#{idx} Tool: `{ev.get('tool')}`**")
                            col_arg, col_res = st.columns(2)
                            with col_arg:
                                st.caption("Arguments truyền vào:")
                                st.json(ev.get("args", {}))
                            with col_res:
                                st.caption("Kết quả trả về từ hệ thống:")
                                st.json(ev.get("result", {}))
                            if idx < len(tool_events):
                                st.divider()

                # Hiển thị câu trả lời cuối cùng
                clean_text, meta_info = format_assistant_reply(ans_text)
                st.markdown(clean_text)
                if meta_info:
                    badges = []
                    if "intent" in meta_info:
                        badges.append(f"🎯 **Intent:** `{meta_info['intent']}`")
                    if "action" in meta_info:
                        badges.append(f"⚡ **Action:** `{meta_info['action']}`")
                    if "evidence_ids" in meta_info:
                        badges.append(f"📁 **Evidence:** `{meta_info['evidence_ids']}`")
                    if badges:
                        st.caption(" | ".join(badges))

                # Lưu vào state messages
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": ans_text,
                    "tool_events": tool_events,
                    "status": status,
                })

                # Ghi nhận transcript ra ổ cứng làm bằng chứng
                turn_record = {
                    "turn": len(st.session_state.messages) // 2,
                    "user_input": user_prompt,
                    "status": status,
                    "assistant_text": ans_text,
                    "rounds": outcome.get("rounds", []),
                    "tool_events": tool_events,
                }
                st.session_state.turn_records.append(turn_record)

                transcript_data = {
                    "session_id": st.session_state.session_id,
                    "provider": provider_choice,
                    "model": model_override,
                    "version": version_choice,
                    "artifact_version": artifact_ver.artifact_version,
                    **hashes,
                    "turns": st.session_state.turn_records,
                }
                write_transcript(transcript_file, transcript_data)

            except Exception as e:
                st.error(f"Lỗi khi thực thi: {str(e)}")