#database/master_contract_db.py

import os
import pandas as pd
import numpy as np
import requests
import gzip
import shutil
import http.client
import json
import pandas as pd
import gzip
import io


from sqlalchemy import create_engine, Column, Integer, String, Float , Sequence, Index
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from dotenv import load_dotenv
from database.auth_db import get_auth_token
from extensions import socketio  # Import SocketIO



load_dotenv()


DATABASE_URL = os.getenv('DATABASE_URL')  # Replace with your database path

engine = create_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

class SymToken(Base):
    __tablename__ = 'symtoken'
    id = Column(Integer, Sequence('symtoken_id_seq'), primary_key=True)
    symbol = Column(String, nullable=False, index=True)  # Single column index
    brsymbol = Column(String, nullable=False, index=True)  # Single column index
    name = Column(String)
    exchange = Column(String, index=True)  # Include this column in a composite index
    brexchange = Column(String, index=True)  
    token = Column(String, index=True)  # Indexed for performance
    expiry = Column(String)
    strike = Column(Float)
    lotsize = Column(Integer)
    instrumenttype = Column(String)
    tick_size = Column(Float)

    # Define a composite index on symbol and exchange columns
    __table_args__ = (Index('idx_symbol_exchange', 'symbol', 'exchange'),)

def init_db():
    print("Initializing Master Contract DB")
    Base.metadata.create_all(bind=engine)

def delete_symtoken_table():
    print("Deleting Symtoken Table")
    SymToken.query.delete()
    db_session.commit()

def copy_from_dataframe(df):
    print("Performing Bulk Insert")
    # Convert DataFrame to a list of dictionaries
    data_dict = df.to_dict(orient='records')

    # Retrieve existing tokens to filter them out from the insert
    existing_tokens = {result.token for result in db_session.query(SymToken.token).all()}

    # Filter out data_dict entries with tokens that already exist
    filtered_data_dict = [row for row in data_dict if row['token'] not in existing_tokens]

    # Insert in bulk the filtered records
    try:
        if filtered_data_dict:  # Proceed only if there's anything to insert
            db_session.bulk_insert_mappings(SymToken, filtered_data_dict)
            db_session.commit()
            print(f"Bulk insert completed successfully with {len(filtered_data_dict)} new records.")
        else:
            print("No new records to insert.")
    except Exception as e:
        print(f"Error during bulk insert: {e}")
        db_session.rollback()


def download_csv_aliceblue_data(output_path):

    print("Downloading Master Contract CSV Files")
    # URLs of the CSV files to be downloaded
    csv_urls = {
        "NSE": "https://v2api.aliceblueonline.com/restpy/static/contract_master/NSE.csv",
    }
    
    # Create a list to hold the paths of the downloaded files
    downloaded_files = []

    # Iterate through the URLs and download the CSV files
    for key, url in csv_urls.items():
        # Send GET request
        response = requests.get(url,timeout=10)
        # Check if the request was successful
        if response.status_code == 200:
            # Construct the full output path for the file
            file_path = f"{output_path}/{key}.csv"
            # Write the content to the file
            with open(file_path, 'wb') as file:
                file.write(response.content)
            downloaded_files.append(file_path)
        else:
            print(f"Failed to download {key} from {url}. Status code: {response.status_code}")
    
def reformat_symbol_detail(s):
    parts = s.split()  # Split the string into parts
    # Reorder and format the parts to match the desired output
    # Assuming the format is consistent and always "Name DD Mon YY FUT"
    return f"{parts[0]}{parts[3]}{parts[2].upper()}{parts[1]}{parts[4]}"

def process_aliceblue_nse_csv(path):
    """
    Processes the aliceblue CSV file to fit the existing database schema and performs exchange name mapping.
    """
    print("Processing aliceblue NSE CSV Data")
    file_path = f'{path}/NSE.csv'

    df = pd.read_csv(file_path)

    filter_df = df[df['Group Name'].isin(['EQ'])]

    token_df = pd.DataFrame()

    token_df['symbol'] = filter_df['Symbol']
    token_df['brsymbol'] = filter_df['Trading Symbol']
    token_df['name'] = filter_df['Instrument Name']
    token_df['exchange'] = filter_df['Exch']
    token_df['brexchange'] = filter_df['Exch']
    token_df['token'] = filter_df['Token']
    token_df['expiry'] = ''
    token_df['strike'] = 1.0
    token_df['lotsize'] = filter_df['Lot Size']
    token_df['instrumenttype'] = 'EQ'
    token_df['tick_size'] = filter_df['Tick Size']
    
    return token_df


def delete_aliceblue_temp_data(output_path):
    # Check each file in the directory
    for filename in os.listdir(output_path):
        # Construct the full file path
        file_path = os.path.join(output_path, filename)
        # If the file is a CSV, delete it
        if filename.endswith(".csv") and os.path.isfile(file_path):
            os.remove(file_path)
            print(f"Deleted {file_path}")
    

def master_contract_download():
    print("Downloading Master Contract")    

    output_path = 'tmp'
    try:
        download_csv_aliceblue_data(output_path)
        delete_symtoken_table()
        token_df = process_aliceblue_nse_csv(output_path)
        copy_from_dataframe(token_df)
        delete_aliceblue_temp_data(output_path)
        
        return socketio.emit('master_contract_download', {'status': 'success', 'message': 'Successfully Downloaded'})
    
    except Exception as e:
        print(str(e))
        return socketio.emit('master_contract_download', {'status': 'error', 'message': str(e)})

def search_symbols(symbol, exchange):
    return SymToken.query.filter(SymToken.symbol.like(f'%{symbol}%'), SymToken.exchange == exchange).all()

