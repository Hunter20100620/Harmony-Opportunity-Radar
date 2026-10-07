# -*- coding: utf-8 -*-
"""流水线子包: capture / probe / audit 各阶段的编排逻辑。"""


# 退出码约定（CLI 与 WebUI 共享的单一来源，禁止各处重复定义）
EXIT_OK = 0            # 成功
EXIT_ABORT = 1         # 熔断/致命失败，重试无益
EXIT_RETRYABLE = 2     # 可重试中断（网络抖动 / LLM 临时故障）


class PipelineAbortError(Exception):
    """熔断异常：产出为空或全部调用失败时抛出，禁止覆写有效历史产物。

    CLI 捕获后以非零退出码结束；WebUI 据此展示失败态。
    """


class RetryableAbortError(PipelineAbortError):
    """可重试的中断：网络抖动 / LLM 临时故障等，重试后可能恢复。

    与普通熔断区分，CLI 以退出码 2 结束；WebUI 据此自动重试若干次，
    仍失败则进入「中断」态，用户修好配置后可一键从失败步骤继续。
    """