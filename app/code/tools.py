import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.dispatcher import AgentTool
from app.code.repository import CodeIndex

SEARCH_LIMIT = 20
READ_WINDOW = 40


class CodeSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=3, max_length=100)


class CodeReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str = Field(min_length=3, max_length=300)
    line: int = Field(ge=1)


class CodeTools:
    def __init__(self, index: CodeIndex):
        self.index = index

    async def search_code(self, text: str) -> dict[str, Any]:
        hits = self.index.search(re.compile(re.escape(text), re.IGNORECASE), limit=SEARCH_LIMIT)
        return {"ok": True, "hits": [{"file": source.relative, "line": number, "text": line[:200]} for source, number, line in hits]}

    async def read_code(self, file: str, line: int) -> dict[str, Any]:
        source = next((item for item in self.index.files if item.relative == file), None)
        if source is None:
            return {"ok": False, "error_code": "NOT_FOUND", "message": "Dosya kod indeksinde yok; search_code sonucundaki yolu kullanın"}
        start = max(0, line - 1 - READ_WINDOW // 4)
        excerpt = source.lines[start : start + READ_WINDOW]
        return {"ok": True, "file": file, "start_line": start + 1, "code": "\n".join(excerpt)}

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool("search_code", "Servis kaynak kodunda metin arar (sınıf, metot, tablo veya kolon adı)", CodeSearchArgs, self.search_code),
            AgentTool("read_code", "Kaynak kod dosyasından bir satır çevresini okur", CodeReadArgs, self.read_code),
        ]
