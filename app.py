import eventlet 
# import eventlet.hubs
eventlet.monkey_patch()  # patch for eventlet async support
# eventlet.hubs.use_hub("eventlet.hubs.asyncio")
from flask import Flask, g, render_template, request, jsonify ,abort,Response
from functools import wraps 
from flask_mqtt import Mqtt 
from flask_socketio import SocketIO, emit, join_room
import pymysql 
import re
import subprocess
import time
import json
from ftplib import FTP
import requests
import os
from dotenv import load_dotenv

from werkzeug.utils import secure_filename
import io
# pip install -r requirements.txt

load_dotenv()  # <-- load .env first

app = Flask(__name__)

# FTP 
FTP_HOST = os.getenv('FTP_HOST') 
FTP_USER = os.getenv('FTP_USER') 
FTP_PASS = os.getenv('FTP_PASS') 
# TARGET_FILE = 'example.txt'  # File to check on FTP server
# TARGET_DIR = '/'             # Directory to look in (optional)

app.config['MQTT_BROKER_URL'] = os.getenv('MQTT_BROKER_URL')   # public broker
app.config['MQTT_BROKER_PORT'] = 1883
app.config['MQTT_USERNAME'] = ''  # set if needed
app.config['MQTT_PASSWORD'] = ''  # set if needed
app.config['MQTT_KEEPALIVE'] = 60
app.config['MQTT_TLS_ENABLED'] = False

mqtt = Mqtt(app)

# Subscribe topic
SUBSCRIBE_TOPIC = 'meow/test_sub'
socketio = SocketIO(app, cors_allowed_origins='*')

messages = []
subscribed_topics = set()

def connect_db():
    return pymysql.connect(
        host=os.getenv('FTP_HOST') ,
        user=os.getenv('db_user') ,
        password=os.getenv('db_pass'),
        db='mdbiot_com',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

def get_db():
    if 'db' not in g:
        g.db = connect_db()
    else:
        try:
            g.db.ping(reconnect=True)
        except:
            g.db = connect_db()
    return g.db

def get_cursor():
    return get_db().cursor()

def ftp_file_exists(filename, directory='/'):
    try:
        ftp = FTP(FTP_HOST, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(directory)
        files = ftp.nlst()  # List of file names
        print(files)
        ftp.quit()
        return filename in files
    except Exception as e:
        print("FTP error:", e)
        return False
# ////////////////////////////////////////////////////////////

@app.route('/check-file')
def check_file():
    exists = ftp_file_exists('rgl.json', '/dconfig/testDL')
    return jsonify({
        "file": 'rgl.json',
        "exists": exists
    })

@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('db', None)
    if db:
        db.close()


# Callback: when MQTT client connects to broker
@mqtt.on_connect()
def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result code {rc}")
    # mqtt.subscribe(SUBSCRIBE_TOPIC)

# Callback: when a message arrives on subscribed topics
@mqtt.on_message()
def on_message(client, userdata, message):
    msg = message.payload.decode()
    topic = message.topic
    print(f"[MQTT] {topic}: {msg}")
    socketio.emit('mqtt_message', {'topic': topic, 'message': msg})
    # print(f"Received message on {message.topic}: {message.payload.decode()}")

@app.route('/')
def handle_home():
    return render_template('welcome.html')
    # return render_template('template1.html')

@app.route('/debug')
def handle_debug():
    # print("===={}".format(clean))
    clean = ''
    cursor = get_cursor()
    cursor.execute("SELECT device_SN,DT,TS_last,WSData FROM device_status WHERE device_SN LIKE '{}%' ".format(clean))
    result = cursor.fetchall()
    d_arr = [item['device_SN']  for item  in result]
    pattern = r'^{}'.format(clean)  # regex: start with M1
    # print(sorted_people.)
    matched_sns = [sn for sn in d_arr if re.match(pattern, sn)]
    sorted_people = sorted(result, key=lambda person: person["device_SN"])
    return render_template('template1.html',data = matched_sns,data_arr=sorted_people )

@app.route('/debug/<path:anything>')
def handle_anything(anything):
    # return f"You visited: {request.path}"
    try:
        sql = ""
        # with connection.cursor() as cursor:
        clean = (anything.strip('/debug/')).upper() if anything.upper() != 'ALL' else ''
        if anything == 'ALL':
            sql = "SELECT * FROM device_status WHERE 1"
        else:
            sql = "SELECT * FROM device_status WHERE device_SN LIKE '{}%'".format(clean)

        print(sql)
        cursor = get_cursor()
        cursor.execute("{}".format(sql))
        result = cursor.fetchall()
        d_arr = [item['device_SN']  for item  in result]

        
        if clean in ['M1','RL','SI','IR','']:
            # Filter values starting with "M1"
            pattern = r'^{}'.format(clean)  # regex: start with M1
            # print(sorted_people.)
            matched_sns = [sn for sn in d_arr if re.match(pattern, sn)]
            sorted_people = sorted(result, key=lambda person: person["device_SN"])
            return render_template('template1.html',data = matched_sns,data_arr=sorted_people )
        else:
            return render_template('template1.html') 

        # return render_template('index.html') 
        
    except Exception as e:
        print('Except as {}'.format(e))
        return render_template('template1.html') 

@app.route('/dashboard', methods=['GET', 'POST'])
def handle_dashboard():
    st = time.time()
    device_sn = request.args.get("device_sn")
    cursor = get_cursor()
    cursor.execute("SELECT DT,TS,DataA FROM log05 WHERE device_SN = '{}' ORDER BY DT DESC LIMIT 10".format(device_sn))
    result = cursor.fetchall()
    print(time.time() - st)
    # print(jsonify(result))
    # print(result)
    # print(device_sn)
    return render_template('vx_dashboard.html',rows=result,device_sn=device_sn)

@app.route('/mqtt', methods=['GET', 'POST'])
def mx_mqtt():
    message = None
    subTopic=""
    if request.method == 'POST':
        data = request.get_json()
        subTopic = data.get("subTopic")
    topic = request.form.get('topic')
    device_sn = request.args.get('device_sn')
    cmd_on = '{ "mode": "cmd", "data":{"VPN":1}, "cmd_id": "C1728370820091" }'
    cmd_off = '{ "mode": "cmd", "data":{"VPN":0}, "cmd_id": "C1728370820091" }'
    cmd_check = '{ "mode": "cmd", "data":{"VPN":2}, "cmd_id": "C1728370820091" }'
    # print(device_sn,topic)

    return render_template('mx_mqttmanage.html', message=device_sn,topic=topic,cmd_on=cmd_on,cmd_off=cmd_off,cmd_check=cmd_check)

@app.route("/ftp_py/<path:filepath>")
def preview_ftp_py(filepath):
    FTP_BASE = "/mainfile_M1"

    # prevent path traversal
    if ".." in filepath:
        abort(403)

    # allow only text/code files
    ALLOWED_EXT = (".py", ".json", ".php", ".txt", "ovpn")
    if not filepath.lower().endswith(ALLOWED_EXT):
        abort(403)

    ftp = FTP(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)

    full_path = f"{FTP_BASE}/{filepath}"

    buffer = io.BytesIO()

    try:
        # 🔒 ensure it's NOT a directory
        try:
            ftp.cwd(full_path)
            ftp.quit()
            abort(403)  # it's a directory
        except:
            pass

        ftp.retrbinary(f"RETR {full_path}", buffer.write)

    except Exception as e:
        ftp.quit()
        abort(404)

    ftp.quit()
    buffer.seek(0)

    return Response(
        buffer.read(),
        mimetype="text/plain; charset=utf-8"
    )
   
@app.route("/ftp_delete/<filename>", methods=["DELETE"])
def delete_ftp_file(filename):
    filename = secure_filename(filename)
    FTP_DIR  = "/mainfile_M1"   # directory to list
    ftp = FTP(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.cwd(FTP_DIR)

    try:
        ftp.delete(filename)
    except Exception as e:
        ftp.quit()
        abort(404, str(e))

    ftp.quit()
    return jsonify({"status": "ok", "file": filename})

@app.route('/config', methods=['GET', 'POST'])
def mx_config():
    
    message = None
    subTopic=""
    if request.method == 'POST':
        data = request.get_json()
        subTopic = data.get("subTopic")
        print(data) 
    topic = request.form.get('topic')
    device_sn = request.args.get('device_sn')
    cmd_on = '{ "mode": "cmd", "data":{"VPN":1}, "cmd_id": "C1728370820091" }'
    cmd_off = '{ "mode": "cmd", "data":{"VPN":0}, "cmd_id": "C1728370820091" }'
    cmd_check = '{ "mode": "cmd", "data":{"VPN":2}, "cmd_id": "C1728370820091" }'
   
    # def get_ftp_files():
    #     FTP_DIR  = "/mainfile_M1"   # directory to list
    #     ftp = FTP(FTP_HOST)
    #     ftp.login(FTP_USER, FTP_PASS)
    #     ftp.cwd(FTP_DIR)
    #     files = ftp.nlst()
    #     ftp.quit()
    #     return files
    def get_ftp_files(_DIR):
        # FTP_DIR = "/mainfile_M1"

        ftp = FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(_DIR)

        items = []
        names = ftp.nlst()
        pwd = ftp.pwd()

        for name in names:
            if name in ('.', '..'):
                continue

            is_dir = False
            count = None

            try:
                # try enter directory
                ftp.cwd(name)
                is_dir = True

                # count files inside directory
                try:
                    count = len([x for x in ftp.nlst() if x not in ('.', '..')])
                except:
                    count = 0

                ftp.cwd(pwd)

            except:
                is_dir = False

            items.append({
                "name": name,
                "is_dir": is_dir,
                "count": count   # None for files
            })

        ftp.quit()

        # ✅ directories first, A–Z
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

        return items

    
    data = {}
    data['is_M1'] = bool(re.search('M1', device_sn))
    data['files'] = get_ftp_files("/mainfile_M1")

    return render_template('mx_mqtt.html', message=device_sn,topic=topic,cmd_on=cmd_on,cmd_off=cmd_off,cmd_check=cmd_check,data=data)


@socketio.on('subscribe')
def handle_subscribe(data):
    topic = data.get('topic')
    print("==",topic)
    if topic and topic not in subscribed_topics:
        mqtt.subscribe(topic)
        subscribed_topics.add(topic)
        print(f"Subscribed to topic: {topic}")
        socketio.emit('mqtt_topic', {'topic': topic, 'message': f"✅ Subscribed to {topic}"})
    else:
        socketio.emit('mqtt_topic', {'topic': topic, 'message': f"⚠️ Already subscribed to {topic}"})

@socketio.on('unsubscribe')
def handle_unsubscribe(data):
    topic = data.get('topic')
    print("==",topic)
    if topic and topic in subscribed_topics:
        mqtt.unsubscribe(topic)
        print(subscribed_topics)
        subscribed_topics.remove(topic)
        print(f"Subscribed to topic: {topic}")
        socketio.emit('mqtt_topic', {'topic': topic, 'message': f"✅ unSubscribed to {topic}"})
    else:
        socketio.emit('mqtt_topic', {'topic': topic, 'message': f"⚠️ Already subscribed to {topic}"})

@socketio.on('sendCMD')
def sendCMD(data):
    send_topic = data.get('send_topic')
    send_message = data.get('send_message')
    mqtt.publish(send_topic, send_message)
    print(f"[PUBLISH] {send_topic} → {send_message}")
    socketio.emit('mqtt_sendCMD', {'topic': send_topic, 'message': f"✅ Send command to {send_message}"})

@socketio.on('openVNC')
def openVNC(data):
    send_message = data.get('send_message')
    print(send_message)
    # Replace with your VNC address and port
    vnc_address = "{}::5900".format(send_message)  # or "192.168.1.100::5901" for some clients
    
    # Example for RealVNC Viewer
    subprocess.Popen(["C:\\Program Files\\RealVNC\\VNC Viewer\\vncviewer.exe", vnc_address])

@socketio.on('connectVPN')
def connectVPN(data):
    # Use GUI-based OpenVPN
    subprocess.run([
        r"C:\Program Files\OpenVPN\bin\openvpn-gui.exe",
        "--connect", "VPN.ovpn"
    ])
  
@socketio.on('closeVPN')
def closeVPN(data):
    subprocess.run(["taskkill", "/IM", "openvpn.exe", "/F"])  

@socketio.on('openSSH')
def closeSSH(data):
    send_message = data.get('send_message')
    print(send_message)
    hostname = "mdbcare@{}".format(send_message)
    subprocess.run(["start", "cmd", "/k", f"ssh {hostname}"], shell=True)
 
@socketio.on('testMSG')
def testMSG(data):
    send_topic = data.get('send_topic')
    send_message = data.get('send_message')
    # send_message = '{ "mode": "cmd", "data":{"EV000003":{"offset_time":{"read":1}}}, "cmd_id": "C1728370820091" }'
    # send_message = '{ "mode": "cmd", "data":{"EV000003":{"offset_time":{"read":1}}}, "cmd_id": "C1728370820091" }'
    mqtt.publish(send_topic, send_message)
    print(f"[PUBLISH] {send_topic} → {send_message}")
    socketio.emit('mqtt_sendCMD2', {'topic': send_topic, 'message': f"✅ Send command to {send_message}"})

# for WebRTC
@app.route('/testWebRTC')
def testWebRTC():
    return render_template('test_webrtc2.html')

@socketio.on("offer")
def handle_offer(data):
    emit("offer", data, broadcast=True, include_self=False)

@socketio.on("answer")
def handle_answer(data):
    emit("answer", data, broadcast=True, include_self=False)

@socketio.on("ice-candidate")
def handle_ice(data):
    emit("ice-candidate", data, broadcast=True, include_self=False)

if __name__ == '__main__':

    socketio.run(app, host='0.0.0.0', port=5000, debug=True)






