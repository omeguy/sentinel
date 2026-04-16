import streamlit as st
import requests
import json

# Configuration
st.set_page_config(page_title="Tracy Sentinel UI", layout="wide")
API_BASE_URL = "http://40.124.81.146:9000" # Your Azure IP

st.title("🛡️ Tracy Bot Orchestrator")

# Sidebar - Session Info
with st.sidebar:
    st.header("Settings")
    user_id = st.text_input("User ID", value="megacorp_admin")
    bot_name = st.text_input("Bot Name", value="demo-bot")
    
    st.divider()
    if st.button("🔌 Check API Health"):
        try:
            res = requests.get(f"{API_BASE_URL}/health")
            st.success(f"API Online: {res.json()}")
        except:
            st.error("API Offline")

# Tabs for different functions
tab1, tab2, tab3 = st.tabs(["📊 Monitor", "🚀 Deploy Bot", "📜 Logs"])

with tab1:
    st.header("Bot Control Center")
    col1, col2, col3 = st.columns(3)
    
    if st.button("🔄 Refresh Status"):
        res = requests.get(f"{API_BASE_URL}/v1/users/{user_id}/bots/{bot_name}")
        st.json(res.json())

    with col1:
        if st.button("▶️ Start Engine", type="primary"):
            res = requests.post(f"{API_BASE_URL}/v1/users/{user_id}/bots/{bot_name}/engine/start")
            st.info("Start signal sent")

    with col2:
        if st.button("⏹️ Stop Engine"):
            res = requests.post(f"{API_BASE_URL}/v1/users/{user_id}/bots/{bot_name}/engine/stop")
            st.warning("Stop signal sent")

with tab2:
    st.header("Deploy New Trading Bot")
    st.write("This sends the full environment payload to the orchestrator.")
    
    # Default Env Payload based on your app.py example
    default_env = {
        "APP_MODE": "dev",
        "MT5_LOGIN": "123456",
        "MT5_SERVER": "Demo-MT5",
        "LOT": "0.01",
        "REDIS_HOST": "tracy-redis",
        "DB_HOST": "tracy-mysql"
    }
    
    env_json = st.text_area("Environment JSON", value=json.dumps(default_env, indent=2), height=300)
    
    if st.button("🚀 Create & Pull Bot Container"):
        payload = {
            "bot_name": bot_name,
            "env": json.loads(env_json),
            "enable_vnc": True,
            "persist_volume": True
        }
        res = requests.post(f"{API_BASE_URL}/v1/users/{user_id}/bots", json=payload)
        st.write(res.json())

with tab3:
    st.header("Live Container Logs")
    tail_count = st.slider("Tail Lines", 10, 500, 100)
    if st.button("Fetch Logs"):
        res = requests.get(f"{API_BASE_URL}/v1/users/{user_id}/bots/{bot_name}/logs?tail={tail_count}")
        st.code(res.text)