class HelloError(Exception):
    """预期内的业务错误。工具层把它变成 MCP 的 isError，不执行任何外部内容。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
