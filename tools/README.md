# tools

维护和手工检查脚本都放在这里，生产入口只保留在 `main.py` 和 `server/walkie_app.py`。

常用命令：

```powershell
python tools\build_vision_profiles.py --overwrite
python tools\check_artifact_pipeline.py --image tests\data\camera\yingguo_yuying.jpg --no-bailian
python tools\check_camera_guide.py --image tests\data\camera\yingguo_yuying.jpg --no-bailian
python tools\check_audio_loop.py --mock-bailian
python tools\check_bailian_app.py --text "这件文物有什么故事？"
python tools\check_http_client_e2e.py --base-url http://127.0.0.1:18080
python tools\check_device_client.py --base-url http://127.0.0.1:18080 --server
python tools\clean_tmp.py
```

`tests/` 只放自动化测试和测试数据；设备、接口、模型链路的人工验收脚本都放在 `tools/check_*`。
