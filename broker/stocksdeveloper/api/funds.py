# api/funds.py for Fyers

import os
import http.client
import json
import urllib.parse

def get_margin_data():

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
    conn.request('POST', '/trading/readPlatformMargins', payload, headers)
    
    # Get the response
    res = conn.getresponse()
    data = res.read()

    response_data = json.loads(data.decode("utf-8"))
    print(f"Status: {response_data['status']}")
    if res.status != 200 or response_data['status'] != True:
        print(f"Error fetching the funds data: {res.status} {res.reason}")
        print(f"Error response message: {response_data['message']}")
        return {}

    utilized = 0.0
    available = 0.0
    total = 0.0
    realised_mtm = 0.0
    unrealised_mtm = 0.0

    if response_data.get('result'):
        for margins in response_data['result']:
            if margins.get('category') == "EQUITY":
                utilized = margins.get('utilized')
                available = margins.get('available')
                total = margins.get('total')
                realised_mtm = margins.get('realisedMtm')
                unrealised_mtm = margins.get('unrealisedMtm')
                break

    # Construct and return the processed margin data
    processed_margin_data = {
        "availablecash": "{:.2f}".format(available),
        "collateral": "0.00",
        "m2munrealized": "{:.2f}".format(unrealised_mtm),
        "m2mrealized": "{:.2f}".format(realised_mtm),
        "utiliseddebits": "{:.2f}".format(utilized),
        "total": "{:.2f}".format(total)
    }
    return processed_margin_data
    