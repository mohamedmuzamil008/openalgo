import http.client
import json
import urllib.parse
import os
from database.token_db import get_token , get_br_symbol, get_symbol
from broker.stocksdeveloper.mapping.transform_data import transform_data , transform_cover_order_data, map_product_type, reverse_map_product_type, transform_modify_order_data

def get_api_response(endpoint, method="POST"):
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')

    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id
    })

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request(method, endpoint, payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))
    
    if res.status != 200 or response_data['status'] != True:
        print(f"Error fetching the order/position/holdings data: {res.status} {res.reason}")
        print(f"Error response message: {response_data['message']}")
        return {}
    
    # Decode and return the response data
    return response_data

def get_order_book(auth):
    return get_api_response("/trading/readPlatformOrders")

def get_trade_book(auth):
    return {}

def get_positions(auth):
    return get_api_response("/trading/readPlatformPositions")

def get_holdings(auth):
    return get_api_response("/trading/readPlatformHoldings")

def get_open_position(tradingsymbol, exchange, producttype):
    positions_data = get_positions()
    
    net_qty = '0'    
    
    if positions_data.get('result'):
        for position in positions_data['result']:
            if position.get('independentSymbol') == tradingsymbol and position.get('exchange') == exchange and position.get('type') == producttype:
                net_qty = (int(position.get('buyQuantity', 0)) - int(position.get('sellQuantity', 0)))
                break  # Assuming you need the first match

    return net_qty

def place_order_api(data):    
    
    api_key = os.getenv('BROKER_API_KEY')
    
    newdata = transform_data(data)     
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode(newdata)
    
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", "/trading/placeRegularOrder", payload, headers)

    res = conn.getresponse()
    data = res.read()
    response_data = json.loads(data.decode("utf-8"))

    if res.status != 200 or response_data['status'] != True:
        orderid = None
        print(f"Error placing the order: {res.status} {res.reason}")
        print(f"Error response message: {response_data['message']}")
    else:
        orderid = response_data['result']
    
    return res, response_data, orderid


def place_cover_order_api(data):    
    
    api_key = os.getenv('BROKER_API_KEY')
    
    newdata = transform_cover_order_data(data)     
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode(newdata)
    
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", "/trading/placeCoverOrder", payload, headers)

    res = conn.getresponse()
    data = res.read()
    response_data = json.loads(data.decode("utf-8"))

    if res.status != 200 or response_data['status'] != True:
        orderid = None
        print(f"Error placing the cover order: {res.status} {res.reason}")
        print(f"Error response message: {response_data['message']}")
    else:
        orderid = response_data['result']
    
    return res, response_data, orderid


def place_smartorder_api(data):

    #If no API call is made in this function then res will return None
    res = None

    # Extract necessary info from data
    symbol = data.get("symbol")
    exchange = data.get("exchange")
    product = data.get("product")
    position_size = int(data.get("position_size", "0"))    

    # Get current open position for the symbol
    current_position = int(get_open_position(symbol, exchange, product))

    print(f"position_size : {position_size}") 
    print(f"Open Position : {current_position}") 
    
    # Determine action based on position_size and current_position
    action = None
    quantity = 0

    # If both position_size and current_position are 0, do nothing
    if position_size == 0 and current_position == 0:
        action = data['action']
        quantity = data['quantity']
        #print(f"action : {action}")
        #print(f"Quantity : {quantity}")
        res, response, orderid = place_order_api(data)
        # print(res)
        # print(response)
        # print(orderid)
        return res , response, orderid
        
    elif position_size == current_position:
        response = {"status": "success", "message": "No action needed. Position size matches current position."}
        orderid = None
        return res, response, orderid  # res remains None as no API call was mad   

    if position_size == 0 and current_position>0 :
        action = "SELL"
        quantity = abs(current_position)
    elif position_size == 0 and current_position<0 :
        action = "BUY"
        quantity = abs(current_position)
    elif current_position == 0:
        action = "BUY" if position_size > 0 else "SELL"
        quantity = abs(position_size)
    else:
        if position_size > current_position:
            action = "BUY"
            quantity = position_size - current_position
            #print(f"smart buy quantity : {quantity}")
        elif position_size < current_position:
            action = "SELL"
            quantity = current_position - position_size
            #print(f"smart sell quantity : {quantity}")

    if action:
        # Prepare data for placing the order
        order_data = data.copy()
        order_data["action"] = action
        order_data["quantity"] = str(quantity)

        #print(order_data)
        # Place the order
        res, response, orderid = place_order_api(order_data)
        #print(res)
        print(response)
        print(orderid)
        
        return res , response, orderid


def close_all_positions():    

    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id,
        'category': 'NET'
    })   

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", '/trading/squareOffPortfolio', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))

    if response_data['result'] == True and response_data['status'] == True:
        return {"status": "success", "message": "All open positions squaredoff"}, 200
    else:
        return {"status": "error", "message": response_data.get("message", "Failed to close all positions")}, 200

def close_position(data):    

    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id,
        'category': 'NET',
        'type': data['product'],
        'exchange': data['exchange'],
        'symbol': data['symbol']
    })   

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", '/trading/squareOffPosition', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))

    if response_data['result'] == True and response_data['status'] == True:
        return {"status": "success", "message": "Position squaredoff successfully"}, 200
    else:
        return {"status": "error", "message": response_data.get("message", "Failed to close the position!")}, 200


def cancel_order(orderid):    
    
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')

    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id,
        'platformId': orderid
    })

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", '/trading/cancelOrderByPlatformId', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))

    if response_data['result'] == True and response_data['status'] == True:
        return {"status": "success", "message": "Order cancelled successfully"}, 200
    else:
        return {"status": "error", "message": response_data.get("message", "Failed to cancel order")}, 200
    
def modify_order(data):

    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')

    newdata = transform_modify_order_data(data)  
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode(newdata)

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", '/trading/modifyOrderByPlatformId', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))

    if response_data['result'] == True and response_data['status'] == True:
        return {"status": "success", "message": "Order modified sucessfully"}, 200
    else:
        return {"status": "error", "message": response_data.get("message", "Failed to modify order")}, 200


def cancel_all_orders_api():  
    
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')
    
    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id
    })   

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request("POST", '/trading/cancelAllOrders', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))

    if response_data['result'] == True and response_data['status'] == True:
        return {"status": "success", "message": "All orders cancelled successfully"}, 200
    else:
        return {"status": "error", "message": response_data.get("message", "Failed to cancel all orders")}, 200

  

    
