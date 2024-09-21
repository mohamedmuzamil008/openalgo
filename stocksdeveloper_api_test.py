import http.client
import json
import urllib.parse
import os

def get_api_response(endpoint, method="POST"):
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    api_key = os.getenv('BROKER_API_KEY')

    headers = {
        'api-key': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    payload = urllib.parse.urlencode({
        'pseudoAccount': pseudo_account_id,
    })

    # Construct the full URL
    conn = http.client.HTTPSConnection("api.stocksdeveloper.in")
    conn.request(method, endpoint, payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()
    
    if res.status != 200:
        print(f"Error fetching the data: {res.status} {res.reason}")
        return {}
    
    # Decode and return the response data
    return json.loads(data.decode("utf-8"))

def get_order_book():
    return get_api_response("/trading/readPlatformOrders")

def get_positions():
    return get_api_response("/trading/readPlatformPositions")

def get_holdings():
    return get_api_response("/trading/readPlatformHoldings")

# Example call to the function
holdings = get_holdings()
print(holdings)
