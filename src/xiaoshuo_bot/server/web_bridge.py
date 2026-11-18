from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
import uvicorn
import json
import asyncio
from src.xiaoshuo_bot.engine import NovelGenerationEngine

app = FastAPI()
# 初始化引擎
engine = NovelGenerationEngine(project_dir='/opt/novel-robot/projects/baiwu_dukou')

HTML_UI = """
<!DOCTYPE html>
<html>
<head>
    <title>小说机器人 · 生产指挥中心</title>
    <script src='https://cdn.tailwindcss.com'></script>
</head>
<body class='bg-gray-900 text-gray-100 min-h-screen flex flex-col'>
    <header class='p-4 border-b border-gray-700 flex justify-between'>
        <h1 class='text-xl font-bold'>小说机器人 · 工业生产中枢</h1>
        <span id='status' class='text-green-500'>● 生产就绪</span>
    </header>
    <main class='flex-grow flex p-4 gap-4'>
        <div class='w-1/3 bg-gray-800 p-4 rounded-lg'>
            <h2 class='font-bold mb-2'>资产流转与状态</h2>
            <div id='pipeline' class='text-sm space-y-2 text-gray-400'>等待指挥官指令...</div>
        </div>
        <div class='w-2/3 bg-gray-800 p-4 rounded-lg flex flex-col'>
            <div id='chat' class='flex-grow overflow-y-auto mb-4 space-y-2 text-sm'></div>
            <input id='cmd' class='w-full p-2 bg-gray-700 rounded border border-gray-600' placeholder='例如：启动创作流程...'>
        </div>
    </main>
    <script>
        const ws = new WebSocket('ws://' + window.location.host + '/ws');
        const chat = document.getElementById('chat');
        const pipe = document.getElementById('pipeline');
        ws.onmessage = (e) => {
            const data = JSON.parse(e.data);
            if(data.type === 'log') chat.innerHTML += `<p class='text-blue-300'>[Robot]: ${data.msg}</p>`;
            if(data.type === 'status') pipe.innerHTML = `<p class='text-green-400'>${data.msg}</p>`;
        };
        document.getElementById('cmd').onkeydown = (e) => {
            if(e.key === 'Enter') {
                ws.send(JSON.stringify({cmd: e.target.value}));
                e.target.value = '';
            }
        };
    </script>
</body>
</html>
"""

@app.get('/')
def get_ui(): return HTMLResponse(HTML_UI)

@app.websocket('/ws')
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({'type': 'log', 'msg': '指挥中心在线，生产引擎已挂载。输入指令即可开始。'})
    while True:
        data = await websocket.receive_json()
        cmd = data.get('cmd', '')
        await websocket.send_json({'type': 'log', 'msg': f'收到指令: {cmd}，正在校验资产...'})
        # 这里模拟触发引擎逻辑
        await websocket.send_json({'type': 'status', 'msg': '正在运行：资产审计'})
        await asyncio.sleep(2)
        await websocket.send_json({'type': 'log', 'msg': '审计通过，正在启动写手 Agent...'})

if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8080)
