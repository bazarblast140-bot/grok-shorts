import os, sys, json, time, re, subprocess
import requests

def call_llm(prompt, system_prompt="You are a creative writer and short video script generator for kids aged 6-10."):
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    moonshot_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    xai_key = os.environ.get("XAI_API_KEY", "").strip() or os.environ.get("GROK_API_KEY", "").strip()

    # 1. DeepSeek (Primary)
    if deepseek_key:
        print("[LLM] Using DeepSeek API...")
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {deepseek_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=120)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
                print(f"[WARN] DeepSeek error ({r.status_code}): {r.text[:200]}")
                time.sleep(3)
            except Exception as e:
                print(f"[WARN] DeepSeek exception: {e}")
                time.sleep(3)

    # 2. Moonshot (Fallback)
    if moonshot_key:
        print("[LLM] Falling back to Moonshot Kimi API...")
        url = "https://api.moonshot.ai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {moonshot_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "kimi-k3",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=120)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
                print(f"[WARN] Moonshot error ({r.status_code}): {r.text[:200]}")
                time.sleep(3)
            except Exception as e:
                print(f"[WARN] Moonshot exception: {e}")
                time.sleep(3)

    # 3. xAI Grok (Last fallback)
    if xai_key:
        print("[LLM] Falling back to xAI Grok API...")
        url = "https://api.x.ai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {xai_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "grok-beta",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        }
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=120)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"]
                print(f"[WARN] Grok API error ({r.status_code}): {r.text[:200]}")
                time.sleep(3)
            except Exception as e:
                print(f"[WARN] Grok API exception: {e}")
                time.sleep(3)

    raise RuntimeError("PipelineError: No working LLM API key available (DEEPSEEK_API_KEY / MOONSHOT_API_KEY / XAI_API_KEY required).")

def send_telegram_notification(text):
    token = os.environ.get("TG_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT", "").strip()
    if not token or not chat_id:
        print("[TG] Telegram token or chat ID not set, skipping notification.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"[TG] Failed to send Telegram message: {e}")

def main():
    print("--- STARTING KIDS EDUCATIONAL SHORTS PIPELINE (DeepSeek Primary) ---")
    try:
        topic_path = "story_topics.json"
        topic = "Interesting Science Fact"
        if os.path.exists(topic_path):
            with open(topic_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                topics = data.get("topics", [])
                unused = [t for t in topics if not t.get("used")]
                if unused:
                    selected = unused[0]
                    topic = selected.get("topic", topic)
                    selected["used"] = True
                    with open(topic_path, "w", encoding="utf-8") as fw:
                        json.dump(data, fw, indent=2, ensure_ascii=False)

        print(f"[TOPIC] Selected topic: {topic}")

        prompt = f"""Write a 40-second educational script for kids aged 6-10 on the topic: "{topic}".

Rules:
- Language: Hinglish (simple Hindi + easy English words)
- Start with a surprising question
- Explain in 3-4 short easy sentences
- Add 1 fun fact
- End with a positive closing line like "Aaj yeh seekha, kal naya fact aayega!"
- Keep total words under 90
- Make it exciting and easy to understand for kids"""

        script = call_llm(prompt)
        print("[SCRIPT] Script generated successfully!")
        print("-" * 40)
        print(script)
        print("-" * 40)
        
        os.makedirs("run", exist_ok=True)
        with open("run/script.txt", "w", encoding="utf-8") as f:
            f.write(script)
            
        send_telegram_notification(f"🎬 Kids Educational Short Ready!\nTopic: {topic}\nModel: DeepSeek")
        print("[SUCCESS] Pipeline completed successfully!")

    except Exception as e:
        err_msg = f"🎬 Kids Short FAILED\nPipelineError: {e}"
        print(err_msg)
        send_telegram_notification(err_msg)
        sys.exit(1)

if __name__ == "__main__":
    main()
