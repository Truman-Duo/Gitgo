"""Saved tool text uses the same bounded Unicode observation contract."""
from types import SimpleNamespace
import json

import pytest

from backend.core.loop.trace_record import read_record_view


def test_metadata_omits_long_file_and_arguments_but_pages_preserve_every_codepoint():
    content=("const 原文 = '👩‍💻e\u0301';\n"*20000)+"ORIGINAL FILE END"
    original={"arguments":{"path":"old.py","content":"x"*200000},
              "data":{"content":content,"truncated":True,"warnings":[{"message":"RECORDED WARNING"}]},
              "formatted_result":json.dumps({"content":content})}
    read=lambda ref: {"legacy_detail":original}
    metadata=read_record_view(read,"immutable")
    assert len(json.dumps(metadata))<1000 and metadata["file_excerpt"]
    assert metadata["sources"][0]["total_chars"]==len(content)
    parts=[]
    for offset in range(0,len(content),8192):
        page=read_record_view(read,"immutable",source_index=0,offset=offset)
        assert len(page["text"])<=8192 and len(page["before"])<=64 and len(page["after"])<=64
        parts.append(page["text"])
    assert "".join(parts)==content
    assert read_record_view(read,"immutable",source_index=1)["text"]=="RECORDED WARNING"
    for index,offset in [(True,0),(-1,0),(2,0),(0,True),(0,-1),(0,len(content)+1)]:
        with pytest.raises(ValueError):
            read_record_view(read,"immutable",source_index=index,offset=offset)


def test_tool_record_views_use_the_formal_project_scoped_native_boundary(monkeypatch,tmp_path_factory):
    from backend.core.native_host import NativeHost
    from backend.core.loop import trace
    host=NativeHost.__new__(NativeHost)
    host.services=SimpleNamespace(project_list=lambda:[{"name":"fixture","workspace":str(tmp_path_factory)}],
        project_list_archived=lambda:[])
    reads=[]
    def read(workspace,reference):
        reads.append((workspace,reference))
        return {"data":{"content":"ORIGINAL FILE"},"formatted_result":"transport wrapper"}
    monkeypatch.setattr(trace,"read_trace_detail",read)
    metadata=host._runtime_trace("fixture",{"action":"detail","ref":"saved","view":"record_sources"})
    assert metadata["detail"]["representation"]=="trace_record_sources"
    page=host._runtime_trace("fixture",{"action":"detail","ref":"saved","view":"record_source","source_index":0})
    assert page["detail"]["text"]=="ORIGINAL FILE"
    assert reads==[(tmp_path_factory.resolve(),"saved")]*2
