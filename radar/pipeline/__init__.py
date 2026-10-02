# -*- coding: utf-8 -*-
"""流水线子包: capture / probe / audit 各阶段的编排逻辑。"""


class PipelineAbortError(Exception):
    """熔断异常：产出为空或全部调用失败时抛出，禁止覆写有效历史产物。

    CLI 捕获后以非零退出码结束；WebUI 据此展示失败态。
    """