import asyncio
import json
import os
import re
import urllib.parse
import uuid
from collections.abc import AsyncGenerator

import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

from client import AgentClient, AgentClientError
from schema import ChatHistory, ChatMessage
from schema.task_data import TaskData, TaskDataStatus
from voice import VoiceManager

# A Streamlit app for interacting with the langgraph agent via a simple chat interface.
# The app has three main functions which are all run async:

# - main() - sets up the streamlit app and high level structure
# - draw_messages() - draws a set of chat messages - either replaying existing messages
#   or streaming new ones.
# - handle_feedback() - Draws a feedback widget and records feedback from the user.

# The app heavily uses AgentClient to interact with the agent's FastAPI endpoints.


APP_TITLE = "Agent Service Toolkit"
APP_ICON = "🧰"
USER_ID_COOKIE = "user_id"

# An agent that has to ask the user for an undecided value ships the options as a
# fenced JSON block so a frontend can render real controls. The wire format is
# the contract (see agents/ratsnestpro/decisions.py); this app deliberately does
# not import the agent package, because it talks to the service over HTTP.
DECISION_FENCE = "ratsnest-decisions"
# Where a submitted form reply waits for the next script run to send it.
_DECISION_REPLY_KEY = "ratsnest_decision_reply"
_DECISION_RE = re.compile(rf"```{DECISION_FENCE}\s*(\{{.*?\}})\s*```", re.DOTALL)


def split_decisions(content: str) -> tuple[str, list[dict]]:
    """Separate displayable prose from the option payload.

    Falling back to "no options, show everything" keeps a malformed or absent
    block from hiding the message: the rendered text always states the same
    question, so losing the controls costs convenience, never information.
    """
    match = _DECISION_RE.search(content or "")
    if not match:
        return content, []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return content, []
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return content, []
    stripped = (content[: match.start()] + content[match.end() :]).strip()
    return stripped, [d for d in decisions if isinstance(d, dict)]


def render_decision_form(decisions: list[dict]) -> str | None:
    """Radio buttons for the open decisions; returns the reply to send, or None.

    The submitted reply is the canonical token form the agent validates, so the
    control is a convenience over typing and never a second, looser protocol.

    The reply is stashed and picked up on the FOLLOWING run rather than returned
    from the run that handled the click. A form with ``clear_on_submit`` schedules
    a rerun of its own to reset the widgets, and that rerun used to arrive while
    the agent stream opened by the same run was still iterating: Streamlit closed
    the async generator mid-flight (``GeneratorExit`` then "asynchronous generator
    is already running"), the service saw the client disappear and cancelled the
    graph run. The answered turn was recorded and then never advanced — the form
    came back, the picks were accepted, and nothing else happened. Stashing keeps
    the click cheap so the stream starts on a run with no pending rerun.
    """
    stashed = st.session_state.pop(_DECISION_REPLY_KEY, None)
    if stashed:
        return str(stashed)
    if not decisions:
        return None
    with st.form("ratsnest_decisions", clear_on_submit=True):
        st.caption("选一个选项后提交 · Pick one option per item, then submit")
        picks: list[str] = []
        extras: list[str] = []
        for index, decision in enumerate(decisions, start=1):
            options = [o for o in decision.get("options", []) if isinstance(o, dict)]
            if not options:
                continue
            keys = [str(o.get("key", "")) for o in options]
            labels = [f"{o.get('key')}. {o.get('label')}" for o in options]
            recommended = str(decision.get("recommended_key") or "").upper()
            default_index = keys.index(recommended) if recommended in keys else 0
            # Keyed by position, and reset by ``clear_on_submit`` so the next
            # turn's question starts at its own recommendation. Keying by slot
            # instead breaks that reset: the key vanishes when the next form asks
            # about different slots, and the pending reset then raises.
            slot = str(decision.get("slot", ""))
            picked = st.radio(
                f"{index}. {decision.get('question', '')}",
                labels,
                index=default_index,
                key=f"decision_{index}_choice",
            )
            picks.append(f"PICK: {slot}={keys[labels.index(picked)]}")
            # Shown unconditionally: a form does not re-run between the radio and
            # this widget, so it cannot appear only for the free-text option.
            extras.append(
                st.text_input(
                    "自填数值（选了自填项时才需要） · value for the free-text option",
                    key=f"decision_{index}_text",
                )
            )
        submitted = st.form_submit_button("提交 · Submit")
    if not submitted:
        return None
    lines = picks + [text.strip() for text in extras if text and text.strip()]
    if not lines:
        return None
    st.session_state[_DECISION_REPLY_KEY] = "\n".join(lines)
    st.rerun()
    return None  # unreachable: st.rerun() raises


# The board pipeline emits one workflow event per step under this phase prefix
# (see agents/ratsnestpro/tools.py _checkpoint_pipeline_step).
PIPELINE_PHASE_PREFIX = "pipeline:"
PIPELINE_TOTAL_STEPS = 17

_PIPELINE_STEP_LABELS = {
    "requirements": "需求解析",
    "topology": "拓扑",
    "selection": "选型",
    "schematic_connections": "原理图连接",
    "schematic_pinmap": "引脚映射",
    "schematic_layout": "原理图布局",
    "schematic_materialize": "生成原理图",
    "erc": "ERC",
    "layout_partition": "板面分区",
    "layout_critical": "关键器件布局",
    "layout_general": "整体布局",
    "layout_write": "生成 PCB",
    "route_plan": "布线规划",
    "route_planes": "铺铜层",
    "route_signals": "信号布线",
    "route_fab": "工艺校核",
    "manufacture": "制造输出",
}


class PipelineProgress:
    """One progress bar for the 17 board-building steps.

    Rendering each step as its own ``st.status`` produced seventeen collapsed
    boxes and no sense of how far along a run was, which is what made a working
    pipeline look hung. A blocked step does not stop the bar: the pipeline keeps
    going when ``RATSNESTPRO_CONTINUE_ON_BLOCKED`` is set, so the bar tracks
    progress and counts blocked steps separately instead of failing at the first.
    """

    def __init__(self) -> None:
        self._status = None
        self._bar = None
        self._blocked: list[str] = []

    def update(self, event: dict) -> None:
        phase = str(event.get("phase", ""))
        step = phase[len(PIPELINE_PHASE_PREFIX) :] or "?"
        label = _PIPELINE_STEP_LABELS.get(step, step)
        total = int(event.get("total_steps") or PIPELINE_TOTAL_STEPS) or PIPELINE_TOTAL_STEPS
        done = int(event.get("completed_steps") or 0)
        status = str(event.get("status", ""))
        started = status == "started"
        blocked = status == "blocked"
        if blocked:
            self._blocked.append(label)
        if self._status is None:
            self._status = st.status("制板流程 · Board pipeline", state="running", expanded=True)
            self._bar = self._status.progress(0.0)
        detail = str(event.get("detail", "")).strip()
        if started:
            self._status.write(f"▶ {done + 1}/{total} {label} 开始")
        else:
            mark = "⚠" if blocked else "✓"
            self._status.write(
                f"{mark} {done}/{total} {label}" + (f" — {detail}" if detail else "")
            )
        self._bar.progress(
            min(max(done / total, 0.0), 1.0),
            text=f"{done}/{total} · {label}" + (" 进行中…" if started else ""),
        )
        tail = f"（{len(self._blocked)} 步被拦下）" if self._blocked else ""
        if done >= total and not started:
            self._status.update(
                label=f"制板流程完成 {done}/{total}{tail}",
                state="error" if self._blocked else "complete",
            )
        else:
            self._status.update(label=f"制板中 {done}/{total} · {label}{tail}", state="running")


def get_or_create_user_id() -> str:
    """Get the user ID from session state or URL parameters, or create a new one if it doesn't exist."""
    # Check if user_id exists in session state
    if USER_ID_COOKIE in st.session_state:
        return st.session_state[USER_ID_COOKIE]

    # Try to get from URL parameters using the new st.query_params
    if USER_ID_COOKIE in st.query_params:
        user_id = st.query_params[USER_ID_COOKIE]
        st.session_state[USER_ID_COOKIE] = user_id
        return user_id

    # Generate a new user_id if not found
    user_id = str(uuid.uuid4())

    # Store in session state for this session
    st.session_state[USER_ID_COOKIE] = user_id

    # Also add to URL parameters so it can be bookmarked/shared
    st.query_params[USER_ID_COOKIE] = user_id

    return user_id


async def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        menu_items={},
    )

    # Hide the streamlit upper-right chrome
    st.html(
        """
        <style>
        [data-testid="stStatusWidget"] {
                visibility: hidden;
                height: 0%;
                position: fixed;
            }
        </style>
        """,
    )
    if st.get_option("client.toolbarMode") != "minimal":
        st.set_option("client.toolbarMode", "minimal")
        await asyncio.sleep(0.1)
        st.rerun()

    # Get or create user ID
    user_id = get_or_create_user_id()

    if "agent_client" not in st.session_state:
        load_dotenv()
        agent_url = os.getenv("AGENT_URL")
        if not agent_url:
            host = os.getenv("HOST", "0.0.0.0")
            port = os.getenv("PORT", 8080)
            agent_url = f"http://{host}:{port}"
        try:
            with st.spinner("Connecting to agent service..."):
                st.session_state.agent_client = AgentClient(base_url=agent_url)
        except AgentClientError as e:
            st.error(f"Error connecting to agent service at {agent_url}: {e}")
            st.markdown("The service might be booting up. Try again in a few seconds.")
            st.stop()
    agent_client: AgentClient = st.session_state.agent_client

    # Initialize voice manager (once per session)
    if "voice_manager" not in st.session_state:
        st.session_state.voice_manager = VoiceManager.from_env()
    voice = st.session_state.voice_manager

    if "thread_id" not in st.session_state:
        thread_id = st.query_params.get("thread_id")
        if not thread_id:
            thread_id = str(uuid.uuid4())
            messages = []
        else:
            # Read the agent from the URL so history is fetched through the graph that
            # created the thread.
            resume_agent = st.query_params.get("agent") or agent_client.agent
            try:
                messages: ChatHistory = agent_client.get_history(
                    thread_id=thread_id, agent=resume_agent
                ).messages
            except AgentClientError:
                st.error("No message history found for this Thread ID.")
                messages = []
        st.session_state.messages = messages
        st.session_state.thread_id = thread_id

    # Keep thread_id in the URL so the address bar is directly shareable.
    st.query_params["thread_id"] = st.session_state.thread_id

    # Config options
    with st.sidebar:
        st.header(f"{APP_ICON} {APP_TITLE}")

        ""
        "Full toolkit for running an AI agent service built with LangGraph, FastAPI and Streamlit"
        ""

        if st.button(":material/chat: New Chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.thread_id = str(uuid.uuid4())
            # Clear saved audio when starting new chat
            if "last_audio" in st.session_state:
                del st.session_state.last_audio
            st.rerun()

        with st.popover(":material/settings: Settings", use_container_width=True):
            model_idx = agent_client.info.models.index(agent_client.info.default_model)
            model = st.selectbox("LLM to use", options=agent_client.info.models, index=model_idx)
            agent_list = [a.key for a in agent_client.info.agents]
            agent_idx = agent_list.index(agent_client.info.default_agent)
            # Sync the selection to the ?agent= URL param (dropped when it's the default).
            agent_client.agent = st.selectbox(
                "Agent to use",
                options=agent_list,
                index=agent_idx,
                key="agent",
                bind="query-params",
            )
            use_streaming = st.toggle("Stream results", value=True)
            # Audio toggle with callback: clears cached audio when toggled off
            enable_audio = st.toggle(
                "Enable audio generation",
                value=True,
                disabled=not voice or not voice.tts,
                help="Configure VOICE_TTS_PROVIDER in .env to enable"
                if not voice or not voice.tts
                else None,
                on_change=lambda: (
                    st.session_state.pop("last_audio", None)
                    if not st.session_state.get("enable_audio", True)
                    else None
                ),
                key="enable_audio",
            )

            # Display user ID (for debugging or user information)
            st.text_input("User ID (read-only)", value=user_id, disabled=True)

        @st.dialog("Architecture")
        def architecture_dialog() -> None:
            st.image(
                "https://github.com/JoshuaC215/agent-service-toolkit/blob/main/media/agent_architecture.png?raw=true"
            )
            "[View full size on Github](https://github.com/JoshuaC215/agent-service-toolkit/blob/main/media/agent_architecture.png)"
            st.caption(
                "App hosted on [Streamlit Cloud](https://share.streamlit.io/) with FastAPI service running in [Azure](https://learn.microsoft.com/en-us/azure/app-service/)"
            )

        if st.button(":material/schema: Architecture", use_container_width=True):
            architecture_dialog()

        with st.popover(":material/policy: Privacy", use_container_width=True):
            st.write(
                "Prompts, responses and feedback in this app are anonymously recorded and saved to LangSmith for product evaluation and improvement purposes only."
            )

        @st.dialog("Share/resume chat")
        def share_chat_dialog() -> None:
            # st.context.url is the browser URL (with query string stripped). Rebuild
            # the params, including the agent so the thread resumes through the right graph.
            if not st.context.url:
                st.error("Could not determine the app URL to build a shareable link.")
                return
            query = urllib.parse.urlencode(
                {
                    "thread_id": st.session_state.thread_id,
                    "agent": agent_client.agent,
                    USER_ID_COOKIE: user_id,
                }
            )
            chat_url = f"{st.context.url}?{query}"
            st.markdown(f"**Chat URL:**\n```text\n{chat_url}\n```")
            st.info("Copy the above URL to share or revisit this chat")

        if st.button(":material/upload: Share/resume chat", use_container_width=True):
            share_chat_dialog()

        "[View the source code](https://github.com/JoshuaC215/agent-service-toolkit)"
        st.caption(
            "Made with :material/favorite: by [Joshua](https://www.linkedin.com/in/joshua-k-carroll/) in Oakland"
        )

    # Draw existing messages
    messages: list[ChatMessage] = st.session_state.messages

    if len(messages) == 0:
        match agent_client.agent:
            case "chatbot":
                WELCOME = "Hello! I'm a simple chatbot. Ask me anything!"
            case "interrupt-agent":
                WELCOME = "Hello! I'm an interrupt agent. Tell me your birthday and I will predict your personality!"
            case "research-assistant":
                WELCOME = "Hello! I'm an AI-powered research assistant with web search and a calculator. Ask me anything!"
            case "rag-assistant":
                WELCOME = """Hello! I'm an AI-powered Company Policy & HR assistant with access to AcmeTech's Employee Handbook.
                I can help you find information about benefits, remote work, time-off policies, company values, and more. Ask me anything!"""
            case _:
                WELCOME = "Hello! I'm an AI agent. Ask me anything!"

        with st.chat_message("ai"):
            st.write(WELCOME)

    # draw_messages() expects an async iterator over messages
    async def amessage_iter() -> AsyncGenerator[ChatMessage, None]:
        for m in messages:
            yield m

    await draw_messages(amessage_iter())

    # Render saved audio for the last AI message (if it exists)
    # This ensures audio persists across st.rerun() calls
    if (
        voice
        and enable_audio
        and "last_audio" in st.session_state
        and st.session_state.last_message
        and len(messages) > 0
        and messages[-1].type == "ai"
    ):
        with st.session_state.last_message:
            audio_data = st.session_state.last_audio
            st.audio(audio_data["data"], format=audio_data["format"])

    # An agent turn that ends on undecided data ships its options with the
    # message. Rendering them as radio buttons is the difference between "answer
    # this question" and "choose one of these": the user does not have to know
    # the answer's format, and the reply comes back as validated tokens.
    pending_decisions: list[dict] = []
    if messages and messages[-1].type == "ai":
        _, pending_decisions = split_decisions(messages[-1].content or "")
    decision_input = render_decision_form(pending_decisions)

    # Generate new message if the user provided new input
    # Use voice manager if available, otherwise fall back to regular input
    # REQUIRED: Set VOICE_STT_PROVIDER, VOICE_TTS_PROVIDER, OPENAI_API_KEY
    # in app .env (NOT service .env) to enable voice features.
    if voice:
        user_input = voice.get_chat_input()
    else:
        user_input = st.chat_input()
    user_input = user_input or decision_input

    if user_input:
        messages.append(ChatMessage(type="human", content=user_input))
        st.chat_message("human").write(user_input)
        try:
            if use_streaming:
                stream = agent_client.astream(
                    message=user_input,
                    model=model,
                    thread_id=st.session_state.thread_id,
                    user_id=user_id,
                )
                await draw_messages(stream, is_new=True)
                # Generate TTS audio for streaming response
                # Note: draw_messages() stores the final message in st.session_state.messages
                # and the container reference in st.session_state.last_message
                if voice and enable_audio and st.session_state.messages:
                    last_msg = st.session_state.messages[-1]
                    # Only generate audio for AI responses with content
                    if last_msg.type == "ai" and last_msg.content:
                        # Use audio_only=True since text was already streamed by draw_messages()
                        voice.render_message(
                            last_msg.content,
                            container=st.session_state.last_message,
                            audio_only=True,
                        )
            else:
                response = await agent_client.ainvoke(
                    message=user_input,
                    model=model,
                    thread_id=st.session_state.thread_id,
                    user_id=user_id,
                )
                messages.append(response)
                # Render AI response with optional voice
                with st.chat_message("ai"):
                    display_text, _options = split_decisions(response.content)
                    if voice and enable_audio:
                        voice.render_message(display_text)
                    else:
                        st.write(display_text)
            st.rerun()  # Clear stale containers
        except AgentClientError as e:
            st.error(f"Error generating response: {e}")
            st.stop()

    # If messages have been generated, show feedback widget
    if len(messages) > 0 and st.session_state.last_message:
        with st.session_state.last_message:
            await handle_feedback()


async def _next_stream_message(
    messages_agen: AsyncGenerator[ChatMessage | str, None],
    is_new: bool,
) -> ChatMessage | str:
    """Read the next protocol message, ignoring live human state echoes."""
    while True:
        message = await anext(messages_agen)
        if is_new and isinstance(message, ChatMessage) and message.type == "human":
            continue
        return message


async def _next_chat_message(
    messages_agen: AsyncGenerator[ChatMessage | str, None],
    is_new: bool,
) -> ChatMessage:
    """Read the next structured message at a tool/handoff protocol boundary."""
    while True:
        message = await _next_stream_message(messages_agen, is_new)
        if isinstance(message, ChatMessage):
            return message


async def draw_messages(
    messages_agen: AsyncGenerator[ChatMessage | str, None],
    is_new: bool = False,
) -> None:
    """
    Draws a set of chat messages - either replaying existing messages
    or streaming new ones.

    This function has additional logic to handle streaming tokens and tool calls.
    - Use a placeholder container to render streaming tokens as they arrive.
    - Use a status container to render tool calls. Track the tool inputs and outputs
      and update the status container accordingly.

    The function also needs to track the last message container in session state
    since later messages can draw to the same container. This is also used for
    drawing the feedback widget in the latest chat message.

    Args:
        messages_aiter: An async iterator over messages to draw.
        is_new: Whether the messages are new or not.
    """

    # Keep track of the last message container
    last_message_type = None
    st.session_state.last_message = None

    # Placeholder for intermediate streaming tokens
    streaming_content = ""
    streaming_placeholder = None
    workflow_statuses = {}
    pipeline_progress = PipelineProgress()

    # Iterate over the messages and draw them
    while True:
        try:
            msg = await _next_stream_message(messages_agen, is_new)
        except StopAsyncIteration:
            break
        # str message represents an intermediate token being streamed
        if isinstance(msg, str):
            # If placeholder is empty, this is the first token of a new message
            # being streamed. We need to do setup.
            if not streaming_placeholder:
                if last_message_type != "ai":
                    last_message_type = "ai"
                    st.session_state.last_message = st.chat_message("ai")
                with st.session_state.last_message:
                    streaming_placeholder = st.empty()

            streaming_content += msg
            streaming_placeholder.write(streaming_content)
            continue
        if not isinstance(msg, ChatMessage):
            st.error(f"Unexpected message type: {type(msg)}")
            st.write(msg)
            st.stop()

        match msg.type:
            # A message from the user, the easiest case
            case "human":
                last_message_type = "human"
                st.chat_message("human").write(msg.content)

            # A message from the agent is the most complex case, since we need to
            # handle streaming tokens and tool calls.
            case "ai":
                # If we're rendering new messages, store the message in session state
                if is_new:
                    st.session_state.messages.append(msg)

                # If the last message type was not AI, create a new chat message
                if last_message_type != "ai":
                    last_message_type = "ai"
                    st.session_state.last_message = st.chat_message("ai")

                with st.session_state.last_message:
                    # If the message has content, write it out.
                    # Reset the streaming variables to prepare for the next message.
                    if msg.content:
                        display_text, _options = split_decisions(msg.content)
                        if streaming_placeholder:
                            streaming_placeholder.write(display_text)
                            streaming_content = ""
                            streaming_placeholder = None
                        else:
                            st.write(display_text)

                    if msg.tool_calls:
                        # Create a status container for each tool call and store the
                        # status container by ID to ensure results are mapped to the
                        # correct status container.
                        call_results = {}
                        for tool_call in msg.tool_calls:
                            # Use different labels for transfer vs regular tool calls
                            if "transfer_to" in tool_call["name"]:
                                label = f"""💼 Sub Agent: {tool_call["name"]}"""
                            else:
                                label = f"""🛠️ Tool Call: {tool_call["name"]}"""

                            status = st.status(
                                label,
                                state="running" if is_new else "complete",
                            )
                            call_results[tool_call["id"]] = status

                        # Expect one ToolMessage for each tool call.
                        for tool_call in msg.tool_calls:
                            if "transfer_to" in tool_call["name"]:
                                status = call_results[tool_call["id"]]
                                status.update(expanded=True)
                                await handle_sub_agent_msgs(messages_agen, status, is_new)
                                break

                            # Only non-transfer tool calls reach this point
                            status = call_results[tool_call["id"]]
                            status.write("Input:")
                            status.write(tool_call["args"])
                            tool_result = await _next_chat_message(
                                messages_agen,
                                is_new,
                            )

                            if tool_result.type != "tool":
                                st.error(f"Unexpected ChatMessage type: {tool_result.type}")
                                st.write(tool_result)
                                st.stop()

                            # Record the message if it's new, and update the correct
                            # status container with the result
                            if is_new:
                                st.session_state.messages.append(tool_result)
                            if tool_result.tool_call_id:
                                status = call_results[tool_result.tool_call_id]
                            status.write("Output:")
                            status.write(tool_result.content)
                            status.update(state="complete")

            case "custom":
                if msg.custom_data.get("kind") == "workflow_event":
                    phase = str(msg.custom_data.get("phase", "workflow"))
                    event_status = str(msg.custom_data.get("status", ""))
                    detail = str(msg.custom_data.get("detail", ""))
                    if phase.startswith(PIPELINE_PHASE_PREFIX):
                        pipeline_progress.update(msg.custom_data)
                        # Kept in history so the bar survives the turn. The app
                        # reruns when a turn ends to clear stale containers, and
                        # that redraw replays st.session_state.messages: an event
                        # that was never stored there vanished with the container,
                        # leaving a finished 17-step build with no record that it
                        # had ever reported progress.
                        if is_new:
                            st.session_state.messages.append(msg)
                        continue
                    if event_status == "started":
                        workflow_statuses[phase] = st.status(
                            phase.replace("-", " ").title(),
                            state="running",
                        )
                    else:
                        phase_status = workflow_statuses.get(phase)
                        if phase_status is None:
                            phase_status = st.status(
                                phase.replace("-", " ").title(),
                                state="running",
                            )
                            workflow_statuses[phase] = phase_status
                        if detail:
                            phase_status.write(detail)
                        phase_status.update(
                            state=(
                                "complete"
                                if event_status in {"completed", "partial", "unavailable"}
                                else "error"
                            )
                        )
                    continue
                # CustomData example used by the bg-task-agent
                # See:
                # - src/agents/utils.py CustomData
                # - src/agents/bg_task_agent/task.py
                try:
                    task_data: TaskData = TaskData.model_validate(msg.custom_data)
                except ValidationError:
                    st.error("Unexpected CustomData message received from agent")
                    st.write(msg.custom_data)
                    st.stop()

                if is_new:
                    st.session_state.messages.append(msg)

                if last_message_type != "task":
                    last_message_type = "task"
                    st.session_state.last_message = st.chat_message(
                        name="task", avatar=":material/manufacturing:"
                    )
                    with st.session_state.last_message:
                        status = TaskDataStatus()

                status.add_and_draw_task_data(task_data)

            # In case of an unexpected message type, log an error and stop
            case _:
                st.error(f"Unexpected ChatMessage type: {msg.type}")
                st.write(msg)
                st.stop()


async def handle_feedback() -> None:
    """Draws a feedback widget and records feedback from the user."""

    # Keep track of last feedback sent to avoid sending duplicates
    if "last_feedback" not in st.session_state:
        st.session_state.last_feedback = (None, None)

    latest_message = next(
        (
            message
            for message in reversed(st.session_state.messages)
            if isinstance(message, ChatMessage)
        ),
        None,
    )
    if latest_message is None:
        return
    latest_run_id = latest_message.run_id
    feedback = st.feedback("stars", key=latest_run_id)
    if latest_run_id is None:
        return

    # If the feedback value or run ID has changed, send a new feedback record
    if feedback is not None and (latest_run_id, feedback) != st.session_state.last_feedback:
        # Normalize the feedback value (an index) to a score between 0 and 1
        normalized_score = (feedback + 1) / 5.0

        agent_client: AgentClient = st.session_state.agent_client
        try:
            await agent_client.acreate_feedback(
                run_id=latest_run_id,
                key="human-feedback-stars",
                score=normalized_score,
                kwargs={"comment": "In-line human feedback"},
            )
        except AgentClientError as e:
            st.error(f"Error recording feedback: {e}")
            st.stop()
        st.session_state.last_feedback = (latest_run_id, feedback)
        st.toast("Feedback recorded", icon=":material/reviews:")


async def handle_sub_agent_msgs(messages_agen, status, is_new):
    """
    This function segregates agent output into a status container.
    It handles all messages after the initial tool call message
    until it reaches the final AI message.

    Enhanced to support nested multi-agent hierarchies with handoff back messages.

    Args:
        messages_agen: Async generator of messages
        status: the status container for the current agent
        is_new: Whether messages are new or replayed
    """
    nested_popovers = {}
    streaming_content = ""
    streaming_placeholder = None

    # looking for the transfer Success tool call message
    try:
        first_msg = await _next_chat_message(messages_agen, is_new)
    except StopAsyncIteration:
        if status:
            status.write("Sub-agent stream ended before the transfer was acknowledged.")
            status.update(state="error")
        return
    if is_new:
        st.session_state.messages.append(first_msg)

    # Continue reading until we get an explicit handoff back
    while True:
        # Read next message
        try:
            sub_msg = await _next_stream_message(messages_agen, is_new)
        except StopAsyncIteration:
            if status:
                status.write("Sub-agent stream ended before control returned.")
                status.update(state="error")
            return

        # A string is an intermediate token from the active sub-agent.
        if isinstance(sub_msg, str):
            if status:
                if not streaming_placeholder:
                    streaming_placeholder = status.empty()
                streaming_content += sub_msg
                streaming_placeholder.write(streaming_content)
            continue
        if not isinstance(sub_msg, ChatMessage):
            if status:
                status.write(f"Unexpected sub-agent message type: {type(sub_msg)}")
                status.update(state="error")
            return

        if is_new:
            st.session_state.messages.append(sub_msg)

        # Handle tool results with nested popovers
        if sub_msg.type == "tool" and sub_msg.tool_call_id in nested_popovers:
            popover = nested_popovers[sub_msg.tool_call_id]
            popover.write("**Output:**")
            popover.write(sub_msg.content)
            continue

        # Handle transfer_back_to tool calls - these indicate a sub-agent is returning control
        if (
            hasattr(sub_msg, "tool_calls")
            and sub_msg.tool_calls
            and any("transfer_back_to" in tc.get("name", "") for tc in sub_msg.tool_calls)
        ):
            # Process transfer_back_to tool calls
            for tc in sub_msg.tool_calls:
                if "transfer_back_to" in tc.get("name", ""):
                    # Read the corresponding tool result
                    try:
                        transfer_result = await _next_chat_message(
                            messages_agen,
                            is_new,
                        )
                    except StopAsyncIteration:
                        if status:
                            status.write("Sub-agent handoff result was not received.")
                            status.update(state="error")
                        return
                    if is_new:
                        st.session_state.messages.append(transfer_result)

            # After processing transfer back, we're done with this agent
            if status:
                status.update(state="complete")
            break

        # Display content and tool calls in the same nested status
        if status:
            if sub_msg.content:
                if streaming_placeholder:
                    streaming_placeholder.write(sub_msg.content)
                    streaming_content = ""
                    streaming_placeholder = None
                else:
                    status.write(sub_msg.content)

            if hasattr(sub_msg, "tool_calls") and sub_msg.tool_calls:
                for tc in sub_msg.tool_calls:
                    # Check if this is a nested transfer/delegate
                    if "transfer_to" in tc["name"]:
                        # Create a nested status container for the sub-agent
                        nested_status = status.status(
                            f"""💼 Sub Agent: {tc["name"]}""",
                            state="running" if is_new else "complete",
                            expanded=True,
                        )

                        # Recursively handle sub-agents of this sub-agent
                        await handle_sub_agent_msgs(messages_agen, nested_status, is_new)
                    else:
                        # Regular tool call - create popover
                        popover = status.popover(f"{tc['name']}", icon="🛠️")
                        popover.write(f"**Tool:** {tc['name']}")
                        popover.write("**Input:**")
                        popover.write(tc["args"])
                        # Store the popover reference using the tool call ID
                        nested_popovers[tc["id"]] = popover


if __name__ == "__main__":
    asyncio.run(main())
