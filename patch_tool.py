import os

def safe_replace(file_path, old_block, new_block):
    if not os.path.exists(file_path):
        print(f"❌ 错误：文件不存在 {file_path}")
        return False
        
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    # 【核心防御逻辑】
    match_count = content.count(old_block)
    if match_count == 0:
        print(f"❌ 错误：未能在 {file_path} 中匹配到目标代码块！请检查上下文。")
        return False
    elif match_count > 1:
        print(f"❌ 错误：在 {file_path} 中匹配到 {match_count} 处相同代码！请扩大 old_block 的范围以确保唯一性。")
        return False
        
    # 执行安全替换
    new_content = content.replace(old_block, new_block)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"✅ 成功：{file_path} 修改已成功应用。")
    return True

# 💡 让 AI 接下来只在这个区域填空：
if __name__ == "__main__":
    TARGET_FILE = "auto-trading-bot/src/signal_handler.py"
    
    # 修复第一处
    old_1 = """            if vol_r < self._cfg.volume_min_ratio * vol_mult:
                logger.info("【成交量过滤】...", ...)
            logger.warning(
                "【第8步B】技术分析失败（%s），降级为市价开仓","""
                
    new_1 = """            if vol_r < self._cfg.volume_min_ratio * vol_mult:
                logger.info("【成交量过滤】...", ...)
                return

        if analysis.get("error"):
            logger.warning(
                "【第8步B】技术分析失败（%s），降级为市价开仓","""
                
    safe_replace(TARGET_FILE, old_1, new_1)