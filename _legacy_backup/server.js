/**
 * AI工具合集 - 导航首页静态服务器
 * 托管本目录 index.html，并允许跨源探测 localhost 各工具端口。
 */
// 复用 unified-pool 的依赖（express/cors），避免本目录单独安装 node_modules
const NODE_DIR = 'c:/Users/Lenovo/CodeBuddy/unified-pool/node_modules';
const express = require(NODE_DIR + '/express');
const cors = require(NODE_DIR + '/cors');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 8080;

app.use(cors());
app.use(express.static(path.join(__dirname)));

app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'index.html')));

app.listen(PORT, () => {
    console.log('AI工具合集导航已启动: http://localhost:' + PORT + '/');
    console.log('（如需一键管理所有服务，请使用本目录的 一键启动.bat / 停止服务.bat）');
});
