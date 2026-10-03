"""Actionable public failure categories; raw exceptions stay in server logs."""

def describe_error(exc):
    name = type(exc).__name__.lower()
    message = str(exc)
    if 'timeout' in name or '逾時' in message:
        return 'timeout', '處理逾時', '請確認來源可存取，或分段後重試；查看服務是否需要重啟'
    if '大小限制' in message or '長度超過' in message:
        return 'limit_exceeded', message, '請縮小檔案或把音訊分段後重新提交'
    if '磁碟' in message or isinstance(exc, OSError) and getattr(exc, 'errno', None) == 28:
        return 'disk_full', '磁碟可用空間不足', '請清理失敗音檔或增加 uploads／暫存磁碟容量後重試'
    if '音訊' in message or '音檔已不存在' in message:
        return 'invalid_audio', message, '請確認檔案可播放，重新上傳 MP3／WAV 等支援格式'
    if 'out of memory' in message.lower():
        return 'gpu_memory', 'GPU 記憶體不足', '請降低 batch_size，確認沒有其他模型占用 GPU 後重試'
    return 'processing_failed', '任務處理失敗', '請確認來源仍可存取，或改用本地上傳；以 job ID 查詢 server logs'
