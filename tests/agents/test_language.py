"""Reply-language detection and its wiring into the agents."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from agents.language import (
    conversation_language,
    detect_language,
    language_directive,
    localized,
    reply_language,
    with_reply_language,
)


class TestDetectLanguage:
    def test_plain_chinese(self) -> None:
        assert detect_language("帮我看一下这个板子的电源部分") == "zh"

    def test_plain_english(self) -> None:
        assert detect_language("Please review the power section of this board") == "en"

    def test_chinese_request_full_of_latin_part_numbers_stays_chinese(self) -> None:
        # The regression this module exists for: Latin identifiers used to
        # outnumber the Chinese characters and flip the reply to English.
        text = "帮我做一个 STM32F103C8T6 LQFP48 最小系统板，USB 供电，加 AMS1117-3.3"
        assert detect_language(text) == "zh"

    def test_english_question_quoting_a_chinese_free_log_stays_english(self) -> None:
        text = "Why does ERC fail here?\n```\nERC: pin not driven by any net\n```"
        assert detect_language(text) == "en"

    def test_windows_path_alone_carries_no_signal(self) -> None:
        assert detect_language(r"C:\projects\board\board.kicad_pro") is None

    def test_punctuation_only_carries_no_signal(self) -> None:
        assert detect_language("?! ...") is None

    def test_short_acknowledgement_carries_no_signal(self) -> None:
        assert detect_language("ok") is None

    def test_japanese_kana_wins_over_shared_han_block(self) -> None:
        assert detect_language("この基板の電源を確認してください") == "ja"

    def test_other_scripts(self) -> None:
        assert detect_language("Проверьте питание платы") == "ru"
        assert detect_language("이 보드의 전원을 확인해 주세요") == "ko"


class TestConversationLanguage:
    def test_latest_decisive_user_turn_wins(self) -> None:
        messages = [
            HumanMessage(content="Design a small STM32 board"),
            AIMessage(content="Sure."),
            HumanMessage(content="改成用 USB 供电"),
        ]
        assert conversation_language(messages) == "zh"

    def test_signal_free_turn_does_not_reset_the_language(self) -> None:
        messages = [
            HumanMessage(content="帮我做一个 STM32 最小系统板"),
            AIMessage(content="好的。"),
            HumanMessage(content="ok"),
        ]
        assert conversation_language(messages) == "zh"

    def test_assistant_turns_are_ignored(self) -> None:
        messages = [
            HumanMessage(content="做一块 STM32 板子"),
            AIMessage(content="RatsNestPro execution report: overall status BLOCKED"),
        ]
        assert conversation_language(messages) == "zh"

    def test_empty_conversation_falls_back_to_default(self) -> None:
        assert conversation_language([]) == "en"

    def test_accepts_plain_dict_messages(self) -> None:
        assert conversation_language([{"role": "user", "content": "查一下这个电阻"}]) == "zh"


class TestReplyLanguageOverride:
    def test_configurable_pins_the_language(self) -> None:
        messages = [HumanMessage(content="Design a small STM32 board")]
        config = {"configurable": {"reply_language": "zh"}}
        assert reply_language(messages, config) == "zh"

    def test_locale_form_is_accepted(self) -> None:
        config = {"configurable": {"reply_language": "zh-CN"}}
        assert reply_language([], config) == "zh"

    def test_unknown_override_falls_back_to_detection(self) -> None:
        messages = [HumanMessage(content="帮我改一下这个原理图")]
        config = {"configurable": {"reply_language": "klingon"}}
        assert reply_language(messages, config) == "zh"


class TestDirectiveAndLocalized:
    def test_directive_names_the_language_and_protects_identifiers(self) -> None:
        directive = language_directive("zh")
        assert "Chinese" in directive
        assert "part " in directive  # identifiers are called out as untranslatable

    def test_unknown_language_falls_back_to_english_directive(self) -> None:
        assert "English" in language_directive("does-not-exist")

    def test_with_reply_language_appends_once(self) -> None:
        prompt = with_reply_language("You are a helpful assistant.", [HumanMessage(content="你好")])
        assert prompt.startswith("You are a helpful assistant.")
        assert "Chinese" in prompt

    def test_localized_falls_back_to_english(self) -> None:
        table = {"en": "blocked", "zh": "已阻塞"}
        assert localized(table, "zh") == "已阻塞"
        assert localized(table, "ja") == "blocked"


class _StubModel:
    """Minimal stand-in whose `bind_tools` returns a real Runnable.

    `FakeListChatModel.bind_tools` raises NotImplementedError, and the assertions
    here only need the assembled system message, not a completion.
    """

    def bind_tools(self, tools):  # noqa: ANN001, ANN202 - test double
        return RunnableLambda(lambda messages: AIMessage(content=""))


class TestAgentWiring:
    def test_research_assistant_system_prompt_follows_the_user(self) -> None:
        from agents.research_assistant import instructions, wrap_model

        runnable = wrap_model(_StubModel())  # type: ignore[arg-type]
        # steps[0] is the preprocessor that assembles the system message.
        prepared = runnable.steps[0].invoke(
            {"messages": [HumanMessage(content="帮我算一下这个分压电阻")]}
        )

        system = prepared[0]
        assert isinstance(system, SystemMessage)
        assert instructions.strip() in system.content
        assert "REPLY LANGUAGE: Chinese" in system.content

    def test_research_assistant_system_prompt_defaults_to_english(self) -> None:
        from agents.research_assistant import wrap_model

        runnable = wrap_model(_StubModel())  # type: ignore[arg-type]
        prepared = runnable.steps[0].invoke(
            {"messages": [HumanMessage(content="Size this divider resistor for me")]}
        )
        assert "REPLY LANGUAGE: English" in prepared[0].content

    def test_configurable_override_reaches_the_system_prompt(self) -> None:
        from agents.research_assistant import wrap_model

        runnable = wrap_model(_StubModel(), {"configurable": {"reply_language": "zh"}})  # type: ignore[arg-type]
        prepared = runnable.steps[0].invoke(
            {"messages": [HumanMessage(content="Size this divider resistor for me")]}
        )
        assert "REPLY LANGUAGE: Chinese" in prepared[0].content

    def test_ratsnestpro_report_headings_follow_the_user(self) -> None:
        from agents.ratsnestpro.ratsnestpro_agent import final_report

        state = {
            "workflow_mode": "research",
            "reply_language": "zh",
            "architecture": {"status": "ok"},
            "trace": [],
            "intent": {},
            "messages": [],
        }
        content = final_report(state)["messages"][0].content  # type: ignore[arg-type]
        assert "# RatsNestPro 执行报告" in content
        # Status tokens stay machine-readable regardless of language.
        assert "**OK**" in content
        assert "`research`" in content

    def test_ratsnestpro_report_defaults_to_english(self) -> None:
        from agents.ratsnestpro.ratsnestpro_agent import final_report

        state = {
            "workflow_mode": "research",
            "architecture": {"status": "ok"},
            "trace": [],
            "intent": {},
            "messages": [HumanMessage(content="Give me a design basis for this board")],
        }
        content = final_report(state)["messages"][0].content  # type: ignore[arg-type]
        assert "# RatsNestPro execution report" in content
