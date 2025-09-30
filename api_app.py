from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import List
import os
import uuid
import imghdr

from app import create_reimbursement_agent

# 启动时全局只创建一次 agent
agent = create_reimbursement_agent()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://engine.pynythd.cn",   # 前端的域名
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/ping")
def ping():
    return {"pong": True}

async def save_tmp(up: UploadFile):
    data = await up.read()
    suffix = os.path.splitext(up.filename or "")[-1].lower()
    p = os.path.join("/tmp", f"upload_{uuid.uuid4().hex}{suffix or '.bin'}")
    with open(p, "wb") as w:
        w.write(data)

    # 判定文件类型（魔数优先）
    head = data[:1024]
    if b"%PDF" in head:
        ftype = "pdf"
    elif imghdr.what(None, h=data) in {"jpeg","png","bmp","gif","tiff","webp"}:
        ftype = "image"
    else:
        # 用文件名/Content-Type兜底
        if suffix == ".pdf" or "pdf" in (up.content_type or "").lower():
            ftype = "pdf"
        else:
            ftype = "image"

    return p, ftype

@app.post("/api/invoices")
async def upload_invoices(files: List[UploadFile] = File(...), note: str = Form("")):
    assert files, "至少上传一个文件"
    # 选择主票据
    main = next(
        (f for f in files if any(k in (f.filename or "").lower() for k in ["发票","invoice","fp","fapiao"])),
        files[0]
    )
    evidences = [f for f in files if f is not main]

    # 保存到临时路径
    main_path, ftype = await save_tmp(main)
    evidence_data = [{"type":"佐证材料","filename":e.filename} for e in evidences]

    result = agent.process_reimbursement(
        file_path=main_path,
        user_input=note,
        evidence_data=evidence_data,   # 关键：把其余文件作为 evidence 传入
        file_type=ftype,   # <- 这里把类型传进去
    )
    return JSONResponse(result)