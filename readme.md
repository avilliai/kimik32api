上游已增加cf turnstile验证     
有能力的自己过，仓库不提供解决方案   
## 安装python
[安装python3.11](https://mirrors.huaweicloud.com/python/3.11.0/python-3.11.0-amd64.exe)

记住第一步勾选add to path就行了，剩下全默认。
## 安装依赖
运行`一键部署脚本.bat`
## 启动
运行`启动脚本.bat`
## 请求
```bash
curl http://127.0.0.1:8077/v1/chat/completions \
  -H "Authorization: Bearer kimik3" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"kimi-k3",
    "stream":false,
    "messages":[{"role":"user","content":"你好"}]
  }'
```
base_url是http://127.0.0.1:8077/v1    
apikey是kimik3    
模型kimi-k3    

无读图能力
