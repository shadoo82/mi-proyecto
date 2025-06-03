from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import requests
import threading
import time
from dotenv import load_dotenv
import os
import websocket
import json

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

load_dotenv()
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

market_data = []
last_update = 0
data_lock = threading.Lock()
orderbook_data = {}
orderbook_hits = {}
orderbook_ages = {}
subscribed_symbols = set()
ws_ticker = None
ws_depth = None

def get_binance_data():
    global market_data, last_update
    try:
        print("Obteniendo datos iniciales de Binance...")
        response = requests.get('https://api.binance.com/api/v3/ticker/24hr', timeout=5)
        response.raise_for_status()
        data = response.json()
        print(f"Datos recibidos, {len(data)} pares encontrados")
        new_data = sorted(
            [item for item in data if item['symbol'].endswith('USDT') and float(item['lastPrice']) > 0],
            key=lambda x: float(x['quoteVolume']),
            reverse=True
        )
        print(f"Pares USDT activos filtrados: {len(new_data)}")
        with data_lock:
            market_data = new_data
            last_update = time.time()
        return new_data
    except Exception as e:
        print(f"Error al obtener datos: {e}")
        return []

def on_ticker_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') == '24hrTicker':
            symbol = data['s']
            price = data['c']
            change = data['P']
            volume = data['q']
            update = {
                'symbol': symbol,
                'price': price,
                'change': change,
                'volume': volume,
                'time': data['E']
            }
            if float(price) > 0:
                print(f"Enviando actualización de precio: {symbol} - price={price}")
                socketio.emit('price_update', update)
                # Actualizar currentPrice en orderbook_data
                if symbol in orderbook_data:
                    orderbook_data[symbol]['currentPrice'] = float(price)
                    # Actualizar las edades
                    if symbol in orderbook_ages:
                        for price in orderbook_ages[symbol]:
                            age = format_age(time.time() - orderbook_ages[symbol][price])
                            for ask in orderbook_data[symbol]['asks']:
                                if ask['price'] == price:
                                    ask['age'] = age
                            for bid in orderbook_data[symbol]['bids']:
                                if bid['price'] == price:
                                    bid['age'] = age
                    socketio.emit('orderbook_update', orderbook_data[symbol])
            else:
                print(f"Símbolo {symbol} tiene precio 0, no enviado")
    except Exception as e:
        print(f"Error en on_ticker_message: {e}")

def on_depth_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') == 'depthUpdate':
            symbol = data['s']
            if symbol not in orderbook_data:
                print(f"Símbolo {symbol} no está en orderbook_data, ignorando...")
                return

            new_asks = data.get('a', [])
            new_bids = data.get('b', [])
            now = time.time()
            if not new_asks and not new_bids:
                print(f"No hay datos de asks ni bids para {symbol}, ignorando...")
                return

            current_asks = {float(ask['price']): ask for ask in orderbook_data[symbol]['asks']}
            current_bids = {float(bid['price']): bid for bid in orderbook_data[symbol]['bids']}
            current_price = orderbook_data[symbol].get('currentPrice', 0)

            # Procesar Asks
            for price, qty in new_asks:
                price = float(price)
                qty = float(qty)
                usdt = price * qty
                if qty == 0:
                    if price in current_asks:
                        del current_asks[price]
                        if symbol in orderbook_hits and price in orderbook_hits[symbol]:
                            del orderbook_hits[symbol][price]
                            del orderbook_ages[symbol][price]
                    continue
                if symbol not in orderbook_hits:
                    orderbook_hits[symbol] = {}
                    orderbook_ages[symbol] = {}
                if price not in orderbook_hits[symbol]:
                    orderbook_hits[symbol][price] = 0
                    orderbook_ages[symbol][price] = now
                orderbook_hits[symbol][price] += 1
                age = format_age(now - orderbook_ages[symbol][price])
                current_asks[price] = {
                    'price': price,
                    'quantity': qty,
                    'usdt': usdt,
                    'age': age,
                    'hits': orderbook_hits[symbol][price],
                    'updated': True
                }

            # Procesar Bids
            for price, qty in new_bids:
                price = float(price)
                qty = float(qty)
                usdt = price * qty
                if qty == 0:
                    if price in current_bids:
                        del current_bids[price]
                        if symbol in orderbook_hits and price in orderbook_hits[symbol]:
                            del orderbook_hits[symbol][price]
                            del orderbook_ages[symbol][price]
                    continue
                if symbol not in orderbook_hits:
                    orderbook_hits[symbol] = {}
                    orderbook_ages[symbol] = {}
                if price not in orderbook_hits[symbol]:
                    orderbook_hits[symbol][price] = 0
                    orderbook_ages[symbol][price] = now
                orderbook_hits[symbol][price] += 1
                age = format_age(now - orderbook_ages[symbol][price])
                current_bids[price] = {
                    'price': price,
                    'quantity': qty,
                    'usdt': usdt,
                    'age': age,
                    'hits': orderbook_hits[symbol][price],
                    'updated': True
                }

            processed_asks = sorted(list(current_asks.values()), key=lambda x: x['usdt'], reverse=True)[:30]
            processed_bids = sorted(list(current_bids.values()), key=lambda x: x['usdt'], reverse=True)[:30]

            diff_processed = len(processed_asks) - len(processed_bids)
            asks_processed = len(processed_asks)
            bids_processed = len(processed_bids)
            total_processed = asks_processed + bids_processed

            orderbook_data[symbol].update({
                'asks': processed_asks,
                'bids': processed_bids,
                'diffProcessed': diff_processed,
                'asksProcessed': asks_processed,
                'bidsProcessed': bids_processed,
                'totalProcessed': total_processed
            })

            print(f"Actualización de libro de órdenes para {symbol}: {len(processed_asks)} asks, {len(processed_bids)} bids")
            socketio.emit('orderbook_update', orderbook_data[symbol])
    except Exception as e:
        print(f"Error en on_depth_message: {e}")

def on_error(ws, error):
    print(f"Error en WebSocket: {error}")

def on_close(ws, close_status_code, close_msg):
    print("WebSocket cerrado:", close_status_code, close_msg)

def on_open(ws):
    print("WebSocket abierto")

def update_orderbook_fallback(symbol):
    while symbol in subscribed_symbols:
        try:
            print(f"Actualizando libro de órdenes para {symbol} mediante API REST...")
            ticker_response = requests.get(f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}', timeout=5)
            ticker_response.raise_for_status()
            ticker_data = ticker_response.json()
            current_price = float(ticker_data.get('price', 0))
            print(f"Precio actual para {symbol} (respaldo): {current_price}")

            response = requests.get(f'https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20', timeout=5)
            response.raise_for_status()
            data = response.json()

            asks = data.get('asks', [])
            bids = data.get('bids', [])
            now = time.time()

            if symbol not in orderbook_data:
                orderbook_data[symbol] = {'asks': [], 'bids': [], 'currentPrice': 0, 'diffProcessed': 0, 'asksProcessed': 0, 'bidsProcessed': 0, 'totalProcessed': 0}

            current_asks = {float(ask['price']): ask for ask in orderbook_data[symbol]['asks']}
            current_bids = {float(bid['price']): bid for bid in orderbook_data[symbol]['bids']}

            for price, qty in asks:
                price = float(price)
                qty = float(qty)
                usdt = price * qty
                if qty == 0:
                    if price in current_asks:
                        del current_asks[price]
                        if symbol in orderbook_hits and price in orderbook_hits[symbol]:
                            del orderbook_hits[symbol][price]
                            del orderbook_ages[symbol][price]
                    continue
                if symbol not in orderbook_hits:
                    orderbook_hits[symbol] = {}
                    orderbook_ages[symbol] = {}
                if price not in orderbook_hits[symbol]:
                    orderbook_hits[symbol][price] = 0
                    orderbook_ages[symbol][price] = now
                orderbook_hits[symbol][price] += 1
                age = format_age(now - orderbook_ages[symbol][price])
                current_asks[price] = {
                    'price': price,
                    'quantity': qty,
                    'usdt': usdt,
                    'age': age,
                    'hits': orderbook_hits[symbol][price],
                    'updated': True
                }

            for price, qty in bids:
                price = float(price)
                qty = float(qty)
                usdt = price * qty
                if qty == 0:
                    if price in current_bids:
                        del current_bids[price]
                        if symbol in orderbook_hits and price in orderbook_hits[symbol]:
                            del orderbook_hits[symbol][price]
                            del orderbook_ages[symbol][price]
                    continue
                if symbol not in orderbook_hits:
                    orderbook_hits[symbol] = {}
                    orderbook_ages[symbol] = {}
                if price not in orderbook_hits[symbol]:
                    orderbook_hits[symbol][price] = 0
                    orderbook_ages[symbol][price] = now
                orderbook_hits[symbol][price] += 1
                age = format_age(now - orderbook_ages[symbol][price])
                current_bids[price] = {
                    'price': price,
                    'quantity': qty,
                    'usdt': usdt,
                    'age': age,
                    'hits': orderbook_hits[symbol][price],
                    'updated': True
                }

            processed_asks = sorted(list(current_asks.values()), key=lambda x: x['usdt'], reverse=True)[:30]
            processed_bids = sorted(list(current_bids.values()), key=lambda x: x['usdt'], reverse=True)[:30]

            diff_processed = len(processed_asks) - len(processed_bids)
            asks_processed = len(processed_asks)
            bids_processed = len(processed_bids)
            total_processed = asks_processed + bids_processed

            orderbook_data[symbol].update({
                'asks': processed_asks,
                'bids': processed_bids,
                'currentPrice': current_price,
                'diffProcessed': diff_processed,
                'asksProcessed': asks_processed,
                'bidsProcessed': bids_processed,
                'totalProcessed': total_processed
            })

            print(f"Actualización de respaldo para {symbol}: {len(processed_asks)} asks, {len(processed_bids)} bids")
            socketio.emit('orderbook_update', orderbook_data[symbol])
            print(f"Evento orderbook_update emitido (respaldo) para {symbol}")
        except Exception as e:
            print(f"Error en la actualización de respaldo para {symbol}: {e}")
        time.sleep(3)

def start_ticker_websocket():
    global ws_ticker
    with data_lock:
        if not market_data:
            get_binance_data()
        for item in market_data:
            symbol = item['symbol'].lower()
            ticker_url = f"wss://stream.binance.com:9443/ws/{symbol}@ticker"
            ws_ticker = websocket.WebSocketApp(
                ticker_url,
                on_message=on_ticker_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            threading.Thread(target=ws_ticker.run_forever, daemon=True).start()
            print(f"Suscrito a ticker WebSocket para {symbol}")
    print(f"Suscrito a {len(market_data)} símbolos para datos de ticker")

def start_depth_websocket(symbol):
    global ws_depth
    symbol_lower = symbol.lower()
    if symbol not in subscribed_symbols:
        depth_url = f"wss://stream.binance.com:9443/ws/{symbol_lower}@depth"
        ws_depth = websocket.WebSocketApp(
            depth_url,
            on_message=on_depth_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open
        )
        threading.Thread(target=ws_depth.run_forever, daemon=True).start()
        print(f"Suscrito a depth WebSocket para {symbol}")
        subscribed_symbols.add(symbol)
        threading.Thread(target=update_orderbook_fallback, args=(symbol,), daemon=True).start()

def format_age(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

@socketio.on('connect')
def handle_connect():
    print("Cliente conectado a Socket.IO")

@socketio.on('disconnect')
def handle_disconnect():
    print("Cliente desconectado de Socket.IO")

@socketio.on('subscribe_orderbook')
def subscribe_orderbook(symbol):
    symbol = symbol.upper()
    print(f"Suscribiendo al libro de órdenes de {symbol}")
    if symbol not in orderbook_data:
        orderbook_data[symbol] = {'asks': [], 'bids': [], 'currentPrice': 0, 'diffProcessed': 0, 'asksProcessed': 0, 'bidsProcessed': 0, 'totalProcessed': 0}
        start_depth_websocket(symbol)
    else:
        print(f"Ya suscrito al libro de órdenes de {symbol}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/orderbook')
def orderbook():
    return render_template('orderbook.html')

@app.route('/get_data')
def get_data():
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        search = request.args.get('search', '').upper()
        start = (page - 1) * per_page
        end = start + per_page
        with data_lock:
            if not market_data:
                return jsonify({"error": "Datos no disponibles, intenta de nuevo más tarde"}), 503
            filtered_data = market_data
            if search:
                filtered_data = [item for item in market_data if search in item['symbol']]
            total_coins = len(filtered_data)
            total_usdt_pairs = len(market_data)
            page_data = filtered_data[start:end] if start < len(filtered_data) else []
            return jsonify({
                'data': page_data,
                'total': total_coins,
                'total_usdt_pairs': total_usdt_pairs,
                'per_page': per_page,
                'page': page,
                'total_pages': (total_coins + per_page - 1) // per_page,
                'last_update': last_update
            })
    except ValueError:
        return jsonify({"error": "Parámetros inválidos"}), 400
    except Exception as e:
        print(f"Error en get_data: {e}")
        return jsonify({"error": "Error interno"}), 500

@app.route('/get_orderbook')
def get_orderbook():
    try:
        symbol = request.args.get('symbol', '').upper()
        if not symbol:
            print("Error: Símbolo no proporcionado en la solicitud")
            return jsonify({"error": "Símbolo no proporcionado"}), 400

        print(f"Obteniendo datos del libro de órdenes para {symbol}...")
        ticker_response = requests.get(f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}')
        ticker_response.raise_for_status()
        ticker_data = ticker_response.json()
        current_price = float(ticker_data.get('price', 0))
        print(f"Precio actual para {symbol}: {current_price}")

        response = requests.get(f'https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20')
        response.raise_for_status()
        data = response.json()
        print(f"Datos del libro de órdenes recibidos para {symbol}: {data}")

        asks = data.get('asks', [])
        bids = data.get('bids', [])
        now = time.time()

        if not asks and not bids:
            print(f"No se encontraron asks ni bids para {symbol}")
            return jsonify({"error": "No se encontraron datos del libro de órdenes"}), 404

        processed_asks = []
        for price, qty in asks:
            price = float(price)
            qty = float(qty)
            usdt = price * qty
            if symbol not in orderbook_hits:
                orderbook_hits[symbol] = {}
                orderbook_ages[symbol] = {}
            if price not in orderbook_hits[symbol]:
                orderbook_hits[symbol][price] = 0
                orderbook_ages[symbol][price] = now
            orderbook_hits[symbol][price] += 1
            age = format_age(now - orderbook_ages[symbol][price])
            processed_asks.append({
                'price': price,
                'quantity': qty,
                'usdt': usdt,
                'age': age,
                'hits': orderbook_hits[symbol][price],
                'updated': True
            })

        processed_bids = []
        for price, qty in bids:
            price = float(price)
            qty = float(qty)
            usdt = price * qty
            if price not in orderbook_hits[symbol]:
                orderbook_hits[symbol][price] = 0
                orderbook_ages[symbol][price] = now
            orderbook_hits[symbol][price] += 1
            age = format_age(now - orderbook_ages[symbol][price])
            processed_bids.append({
                'price': price,
                'quantity': qty,
                'usdt': usdt,
                'age': age,
                'hits': orderbook_hits[symbol][price],
                'updated': True
            })

        processed_asks = sorted(processed_asks, key=lambda x: x['usdt'], reverse=True)[:30]
        processed_bids = sorted(processed_bids, key=lambda x: x['usdt'], reverse=True)[:30]

        diff_processed = len(processed_asks) - len(processed_bids)
        asks_processed = len(processed_asks)
        bids_processed = len(processed_bids)
        total_processed = asks_processed + bids_processed

        orderbook_data[symbol] = {
            'symbol': symbol,
            'asks': processed_asks,
            'bids': processed_bids,
            'currentPrice': current_price,
            'diffProcessed': diff_processed,
            'asksProcessed': asks_processed,
            'bidsProcessed': bids_processed,
            'totalProcessed': total_processed
        }

        print(f"Datos procesados para {symbol}: {len(processed_asks)} asks, {len(processed_bids)} bids")
        return jsonify(orderbook_data[symbol])
    except requests.exceptions.RequestException as e:
        print(f"Error al obtener datos del libro de órdenes para {symbol}: {e}")
        return jsonify({"error": f"Error al obtener datos del libro de órdenes: {str(e)}"}), 500
    except Exception as e:
        print(f"Error en get_orderbook para {symbol}: {e}")
        return jsonify({"error": "Error al obtener datos del libro de órdenes"}), 500

if __name__ == '__main__':
    get_binance_data()
    threading.Thread(target=start_ticker_websocket, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)