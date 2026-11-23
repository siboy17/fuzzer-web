#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2014 Paul Kinsella <kali.gsm.rtl.sdr@gmail.com>
#
# This is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.

import glob
import sys
import threading
import time

from flask import Flask, jsonify, render_template, request
import serial
import serial.tools.list_ports

app = Flask(__name__)

###################################
#   Serial setup global default
###################################
DEFAULT_BAUDRATE = 9600
serial_lock = threading.Lock()

# Konstanta SMS Type (TP-DCS)
FLASH1 = "10"
FLASH2 = "14"
FLASH3 = "18"
SILENT = "C0"
NORMAL = "08"


#################################
# Return serial ports list
#################################
def getOpenSerialPorts():
    result = []
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if p.device and p.device not in result:
            result.append(p.device)

    if not result and sys.platform.startswith("win"):
        for i in range(1, 256):
            port_name = f"COM{i}"
            try:
                s = serial.Serial(port_name, timeout=0.2)
                s.close()
                result.append(port_name)
            except (serial.SerialException, OSError):
                pass

    elif not result and sys.platform.startswith("linux"):
        temp_list = (
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyS*")
        )
        for a_port in temp_list:
            try:
                s = serial.Serial(a_port, timeout=0.2)
                s.close()
                result.append(a_port)
            except (serial.SerialException, OSError):
                pass

    return result if result else ["NO PORTS"]


def read_serial_until(ser_conn: serial.Serial, timeout_sec: float = 2.0) -> str:
    deadline = time.time() + timeout_sec
    chunks = []
    while time.time() < deadline:
        waiting = ser_conn.in_waiting
        if waiting > 0:
            data = ser_conn.read(waiting).decode("utf-8", errors="ignore")
            chunks.append(data)
            joined = "".join(chunks)
            if "OK" in joined or "ERROR" in joined or ">" in joined:
                break
        time.sleep(0.05)
    return "".join(chunks)


########################################################################
#   GET BACK AN AT COMMAND REPLY
########################################################################
def getAtReply(serial_port: str, at_command: str) -> str:
    if not serial_port or serial_port == "NO PORTS":
        return "Error: Port serial tidak valid"
    if not at_command or not at_command.strip():
        return "Error: Perintah AT kosong"

    with serial_lock:
        ser_conn = None
        try:
            ser_conn = serial.Serial(
                port=serial_port,
                baudrate=DEFAULT_BAUDRATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            time.sleep(0.2)
            ser_conn.reset_input_buffer()
            ser_conn.reset_output_buffer()
            ser_conn.write((at_command.strip() + "\r").encode("utf-8"))
            ser_conn.flush()
            return read_serial_until(ser_conn, timeout_sec=2.0)
        except Exception as e:
            return f"Error: {str(e)}"
        finally:
            if ser_conn and ser_conn.is_open:
                ser_conn.close()


def send_pdu_via_serial(serial_port: str, pdu_string: str):
    if not serial_port or serial_port == "NO PORTS":
        raise ValueError("Port serial tidak valid")

    pdu_len = (len(pdu_string) - 2) // 2 if pdu_string.startswith("00") else len(pdu_string) // 2
    trace = []

    with serial_lock:
        ser_conn = None
        try:
            ser_conn = serial.Serial(
                port=serial_port,
                baudrate=DEFAULT_BAUDRATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
            )
            trace.append(f"Open {serial_port} @ {DEFAULT_BAUDRATE} bps")
            time.sleep(0.2)
            ser_conn.reset_input_buffer()
            ser_conn.reset_output_buffer()

            ser_conn.write(b"AT+CMGF=0\r")
            ser_conn.flush()
            cmgf_resp = read_serial_until(ser_conn, timeout_sec=1.5).strip()
            trace.append(f"TX: AT+CMGF=0 | RX: {cmgf_resp or '(no reply)'}")

            ser_conn.write(f"AT+CMGS={pdu_len}\r".encode("ascii"))
            ser_conn.flush()
            prompt_resp = read_serial_until(ser_conn, timeout_sec=2.0).strip()
            trace.append(f"TX: AT+CMGS={pdu_len} | RX: {prompt_resp or '(no reply)'}")

            ser_conn.write((pdu_string + "\x1A").encode("ascii"))
            ser_conn.flush()
            final_resp = read_serial_until(ser_conn, timeout_sec=6.0).strip()
            trace.append(f"TX: <PDU+CtrlZ> | RX: {final_resp or '(no reply)'}")

            combined = (prompt_resp + "\n" + final_resp).strip()
            return combined or "Sent (No response)", pdu_len, trace
        finally:
            if ser_conn and ser_conn.is_open:
                ser_conn.close()
                trace.append(f"Close {serial_port}")


###############################################################################
#   SWAPS NUMBER EVERY 2 DIGITS USED FOR PDU
#############################################################################
def swapNumber(number: str) -> str:
    swaparray = []
    for idx in range(0, len(number) - 1, 2):
        swaparray.append(number[idx + 1])
        swaparray.append(number[idx])
    return "".join(swaparray)


###############################################################################
#   ENCODING HELPERS
#############################################################################
def stringToHex(str2hex: str) -> str:
    return str2hex.encode("utf-8").hex().upper()


def stringToHex18bit(str2hex: str) -> str:
    return str2hex.encode("utf-16-be").hex().upper()


def encode_wap_datetime_bcd(dt_str: str) -> str:
    """
    Mengubah timestamp menjadi 6 byte BCD (YY MM DD HH MM SS) lalu di-swap semi-octet.
    """
    digits = "".join(ch for ch in (dt_str or "") if ch.isdigit())
    digits = digits.ljust(14, "0")[:14]
    yy_mm_dd_hh_mm_ss = digits[2:]
    return swapNumber(yy_mm_dd_hh_mm_ss).upper()


########################################################################
#           CREATE PDU STRING
########################################################################
def createPduString(phone_num: str, sms_text: str, message_type: str, del_report_status: bool) -> str:
    clean_num = "".join(ch for ch in (phone_num or "") if ch.isdigit())
    if not clean_num:
        raise ValueError("Nomor tujuan harus berisi digit angka")

    number_len = len(clean_num)
    padded_num = clean_num + ("F" if number_len % 2 != 0 else "")
    swapped_num = swapNumber(padded_num)
    number_len_hex = f"{number_len:02X}"

    decoded_msg = stringToHex18bit(sms_text or "")
    udl_bytes = len(decoded_msg) // 2
    if udl_bytes > 140:
        raise ValueError("Panjang pesan UCS-2 melebihi 140 byte (maksimal 70 karakter untuk single SMS)")

    msg_len_hex = f"{udl_bytes:02X}"
    del_report = "21" if del_report_status else "01"
    tp_pid = "00"
    tp_dcs = message_type

    return f"00{del_report}00{number_len_hex}91{swapped_num}{tp_pid}{tp_dcs}{msg_len_hex}{decoded_msg}"


########################################################################
#           CREATE WAP PUSH PDU STRING
########################################################################
def createWapPduString(start_date, end_date, header, target_num, msg, wap_type, del_report_status: bool = False):
    clean_num = "".join(ch for ch in (target_num or "") if ch.isdigit())
    if not clean_num:
        raise ValueError("Nomor target WAP Push harus berisi digit angka")

    number_len = len(clean_num)
    number_len_hex = f"{number_len:02X}"
    padded_num = clean_num + ("F" if number_len % 2 != 0 else "")
    swapped_num = swapNumber(padded_num)

    header_hex = stringToHex(header or "")
    msg_hex = stringToHex(msg or "")
    start_bcd = encode_wap_datetime_bcd(str(start_date or ""))
    end_bcd = encode_wap_datetime_bcd(str(end_date or ""))
    clean_wap_type = (str(wap_type or "0B")).strip().upper()

    # PDU-Type dengan TP-UDHI (0x40): 0x61 jika status report aktif, 0x41 jika tidak
    pdu_type = "61" if del_report_status else "41"

    tp_ud = (
        f"0B05040B84C0020003F001010B060403AE81EA02056A0045C6{clean_wap_type}"
        f"03{header_hex}000AC306{start_bcd}10C306{end_bcd}0103{msg_hex}00"
    )
    udl_bytes = len(tp_ud) // 2
    if udl_bytes > 140:
        raise ValueError(f"Payload WAP Push ({udl_bytes} byte) melebihi batas 140 byte untuk single SMS")

    msg_len_hex = f"{udl_bytes:02X}"
    return f"00{pdu_type}00{number_len_hex}91{swapped_num}0004{msg_len_hex}{tp_ud}"


########################################################################
#           FLASK ROUTE ENDPOINTS
########################################################################
@app.route("/")
def index():
    ports = getOpenSerialPorts()
    return render_template("index.html", ports=ports)


@app.route("/api/refresh-ports", methods=["GET"])
def refresh_ports():
    return jsonify({"ports": getOpenSerialPorts()})


@app.route("/api/send-command", methods=["POST"])
def send_command():
    data = request.get_json(silent=True) or {}
    port = data.get("port")
    command = data.get("command")
    response = getAtReply(port, command)
    status = "error" if response.startswith("Error:") or "ERROR" in response.upper() else "success"
    return jsonify({"status": status, "response": response})


@app.route("/api/send-sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    port = data.get("port")
    target = data.get("target")
    msg = data.get("msg", "")
    sms_type_str = data.get("sms_type", "NORMAL")
    del_report = bool(data.get("del_report", True))

    sms_type = NORMAL
    if sms_type_str == "SILENT":
        sms_type = SILENT
    elif sms_type_str == "FLASH":
        sms_type = FLASH3

    try:
        pdu_string = createPduString(target, msg, sms_type, del_report)
        response, pdu_len, trace = send_pdu_via_serial(port, pdu_string)
        has_modem_error = "ERROR" in response.upper()
        return jsonify({
            "status": "error" if has_modem_error else "success",
            "pdu": pdu_string,
            "pdu_len": pdu_len,
            "dcs": sms_type,
            "trace": trace,
            "response": response,
        })
    except Exception as e:
        return jsonify({"status": "error", "response": str(e)})


@app.route("/api/send-wap", methods=["POST"])
def send_wap():
    data = request.get_json(silent=True) or {}
    port = data.get("port")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    header = data.get("header")
    target = data.get("target")
    msg = data.get("msg")
    wap_type = data.get("wap_type")
    del_report = bool(data.get("del_report", False))

    try:
        wap_pdu = createWapPduString(start_date, end_date, header, target, msg, wap_type, del_report)
        response, pdu_len, trace = send_pdu_via_serial(port, wap_pdu)
        has_modem_error = "ERROR" in response.upper()
        return jsonify({
            "status": "error" if has_modem_error else "success",
            "pdu": wap_pdu,
            "pdu_len": pdu_len,
            "trace": trace,
            "response": response,
        })
    except Exception as e:
        return jsonify({"status": "error", "response": str(e)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", debug=False, port=5000)