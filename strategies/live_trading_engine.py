# strategies/live_trading_engine.py
from colorama import Fore, Style, init
init(autoreset=True)  # Initialize colorama

from live_config import TRADING_CONFIG

import pandas as pd
from datetime import datetime, timedelta, time
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import execute_batch
from psycopg2 import pool
from kafka import KafkaConsumer
from kafka.errors import KafkaError
import logging
import traceback
import random
import os
import pytz
import threading
from itertools import product, groupby
import time as time_module
import numpy as np
from collections import defaultdict, deque
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from openalgo import api
import signal
import sys
import talib
import warnings
import re
from colorama import Fore, Style
from services.history_service import get_history_intraday

#with warnings.catch_warnings():
#    warnings.filterwarnings("ignore", category=UserWarning, message=r"^pandas only supports SQLAlchemy connectable")

warnings.simplefilter("ignore", UserWarning)

IST = pytz.timezone('Asia/Kolkata')
UTC = pytz.timezone('UTC')

class PositionManager:
    """Manages open positions and constraints"""
    def __init__(self, api_client, sl_pct, tp_pct, trail_activation_pct, trail_stop_gap_pct, trail_increment_pct, trading_start, trading_end, max_open_positions, max_daily_trades, max_strategy_trades_per_day):
        self.api_client = api_client
        self.open_positions = {}  # {symbol: position_info}
        self.daily_trades = defaultdict(int)  # {date: count}
        self.daily_strategy_trades = defaultdict(lambda: defaultdict(int))  # {date: {strategy: count}}
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.trail_activation_pct = trail_activation_pct
        self.trail_stop_gap_pct = trail_stop_gap_pct
        self.trail_increment_pct = trail_increment_pct
        self.trading_start = trading_start
        self.trading_end = trading_end
        self.max_open_positions = max_open_positions
        self.max_daily_trades = max_daily_trades
        self.max_strategy_trades_per_day = max_strategy_trades_per_day
        self.trade_history = []
        self.lock = threading.RLock()
        self.logger = logging.getLogger(f"PositionManager")

        # Load state on initialization
        self.load_state()
        
    def can_open_position(self, symbol, strategy, current_date):
        """Check if position can be opened based on constraints"""
        with self.lock:
            try:
                # GET OPEN POSITIONS FROM API CLIENT
                position_response = self.api_client.get_positionbook()
                
                # Validate position response
                if isinstance(position_response, tuple) and len(position_response) == 3:
                    success, response_data, status_code = position_response
                    
                    if success and status_code == 200 and isinstance(response_data, dict):
                        # Extract positions data
                        positions_data = response_data.get('data', [])
                        if not isinstance(positions_data, list):
                            colored_log(self.logger, 'error', "Invalid positions data format", success=False)
                            return False, "Unable to fetch current positions"
                        
                        # Validate positions dictionary from response
                        for position in positions_data:
                            if position.get('symbol') and float(position.get('quantity', 0)) != 0 and position['symbol'] not in self.open_positions:
                                # Delete the position from the open positions dictionary
                                del self.open_positions[position['symbol']] 
                        
                        current_date = datetime.now(IST).date()

                        # Validate the today's trades count
                        if len(positions_data) == self.daily_trades[current_date]:      
                            colored_log(self.logger, 'info', "Trade count matching with Broker count", success=True)
                        else:
                            colored_log(self.logger, 'warning', f"Trade count not matching with Broker count. Trade count: {self.daily_trades[current_date]}, Broker count: {len(positions_data)}", success=True)
                            self.daily_trades[current_date] = len(positions_data)
                            colored_log(self.logger, 'info', f"Trade count updated with Broker count", success=True)
                        
                    else:
                        error_msg = response_data.get('message', 'Unknown error') if isinstance(response_data, dict) else str(response_data)
                        colored_log(self.logger, 'error', f"Failed to fetch positions: {error_msg}", success=False)
                        return False, "Unable to fetch current positions"
                else:
                    colored_log(self.logger, 'error', f"Invalid position response format: {position_response}", success=False)
                    return False, "Unable to fetch current positions"

                # Max 3 open positions at a time
                if len(self.open_positions) >= self.max_open_positions:
                    return False, f"Max open positions ({self.max_open_positions}) reached"
                
                # Max 5 trades per day
                if self.daily_trades[current_date] >= self.max_daily_trades:
                    return False, f"Max daily trades ({self.max_daily_trades}) reached"
                
                # Max 1 trade per strategy per day
                if self.daily_strategy_trades[current_date][strategy] >= self.max_strategy_trades_per_day:
                    return False, f"Strategy {strategy} already used today ({self.max_strategy_trades_per_day} times)"
                
                # Cannot open position if already have position in this symbol
                if symbol in self.open_positions:
                    return False, f"Already have position in {symbol}"
                
                # Save state after validation
                self.save_state()
                return True, "OK"
                
            except Exception as e:
                colored_log(self.logger, 'error', f"Error checking position constraints: {e}", success=False)
                return False, "Error checking position constraints"
                    
    def open_position(self, symbol, strategy, direction, entry_price, quantity, timestamp, sl_price, tp_price, trail_activation_price, order_id, sl_order_id):
        """Open new position"""
        with self.lock:
            current_date = timestamp.date()
            
            position_info = {
                'symbol': symbol,
                'strategy': strategy,
                'direction': direction,  # 'LONG' or 'SHORT'
                'entry_price': entry_price,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'trail_activation_price': trail_activation_price,
                'quantity': quantity,
                'entry_time': timestamp,
                'trailing_active': False,
                'trail_stop': None,
                'last_trail_price': None,
                'order_id': order_id,
                'sl_order_id': sl_order_id
            }
            
            self.open_positions[symbol] = position_info
            self.daily_trades[current_date] += 1
            self.daily_strategy_trades[current_date][strategy] += 1

            # Save state after position opened
            self.save_state()
            
            logging.info(f"📈 POSITION OPENED: {symbol} {direction} @ {entry_price} | Strategy: {strategy}")
            return position_info 
    
    def close_position(self, symbol, exit_price, exit_reason, exit_order_id, timestamp):
        """Close existing position"""
        with self.lock:
            if symbol not in self.open_positions:
                return None
            
            position = self.open_positions.pop(symbol)
            
            # Calculate P&L
            is_long = position['direction'] == 'LONG'
            pnl = (exit_price - position['entry_price']) * position['quantity'] if is_long else (position['entry_price'] - exit_price) * position['quantity']
            
            trade_record = {
                'symbol': symbol,
                'strategy': position['strategy'],
                'direction': position['direction'],
                'entry_time': position['entry_time'],
                'entry_price': position['entry_price'],
                'exit_time': timestamp,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'quantity': position['quantity'],
                'sl_price': position['sl_price'],
                'tp_price': position['tp_price'],
                'trail_activation_price': position['trail_activation_price'],
                'gross_pnl': round(pnl, 2),
                'holding_period': timestamp - position['entry_time'],
                'trailing_active': position['trailing_active'],
                'trail_stop': position['trail_stop'],
                'last_trail_price': position['last_trail_price'],
                'order_id': position['order_id'],
                'sl_order_id': position['sl_order_id'],
                'exit_order_id': exit_order_id
            }
            
            self.trade_history.append(trade_record)

            # Save state after position closed
            self.save_state()
            
            logging.info(f"📉 POSITION CLOSED: {symbol} {position['direction']} @ {exit_price} | Reason: {exit_reason} | P&L: {pnl:.2f}")
            return trade_record
    
    def save_state(self):
        """Save position manager state to file"""
        try:
            current_date = datetime.now(IST).date().strftime("%Y%m%d")
            state = {
                'open_positions': self.open_positions,
                'daily_trades': {str(k): v for k, v in self.daily_trades.items()},  # Convert date to string
                'daily_strategy_trades': {str(k): v for k, v in self.daily_strategy_trades.items()},
                'trade_history': self.trade_history
            }
            
            # Create state directory if it doesn't exist
            os.makedirs('state', exist_ok=True)
            
            # Save state to file
            state_file = f'state/position_state_{current_date}.json'
            with open(state_file, 'w') as f:
                json.dump(state, f, default=str)  # Use default=str to handle datetime objects
                
            colored_log(self.logger, 'info', f"Position manager state saved to {state_file}", success=True)
        except Exception as e:
            colored_log(self.logger, 'error', f"Error saving position manager state: {e}", success=False)

    def load_state(self):
        """Load position manager state from file"""
        try:
            current_date = datetime.now(IST).date().strftime("%Y%m%d")
            state_file = f'state/position_state_{current_date}.json'
            
            if not os.path.exists(state_file):
                colored_log(self.logger, 'info', f"No state file found for {current_date}", success=True)
                return
                
            with open(state_file, 'r') as f:
                state = json.load(f)
                
            # Restore state
            self.open_positions = state.get('open_positions', {})
            
            # Convert string dates back to datetime.date objects
            self.daily_trades = defaultdict(int)
            for date_str, count in state.get('daily_trades', {}).items():
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                self.daily_trades[date_obj] = count
                
            # Restore daily strategy trades
            self.daily_strategy_trades = defaultdict(lambda: defaultdict(int))
            for date_str, strategies in state.get('daily_strategy_trades', {}).items():
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                for strategy, count in strategies.items():
                    self.daily_strategy_trades[date_obj][strategy] = count
                    
            # Restore trade history
            self.trade_history = state.get('trade_history', [])
            
            # Convert timestamp strings back to datetime objects in trade history
            for trade in self.trade_history:
                if isinstance(trade.get('entry_time'), str):
                    trade['entry_time'] = datetime.fromisoformat(trade['entry_time'])
                if isinstance(trade.get('exit_time'), str):
                    trade['exit_time'] = datetime.fromisoformat(trade['exit_time'])
                
            colored_log(self.logger, 'info', f"Position manager state loaded from {state_file}", success=True)
        except Exception as e:
            colored_log(self.logger, 'error', f"Error loading position manager state: {e}", success=False)

    def update_trailing_stop(self, symbol, current_price):
        """Update trailing stop for position"""
        with self.lock:
            if symbol not in self.open_positions:
                return False, None
            
            position = self.open_positions[symbol]
            is_long = position['direction'] == 'LONG'          
            
            if not position['trailing_active']:                
                if is_long:
                    if current_price >= position['trail_activation_price']:
                        position['trailing_active'] = True
                        position['trail_stop'] = current_price * (1 - self.trail_stop_gap_pct/100)
                        position['last_trail_price'] = current_price
                        self.save_state()
                        return True, position['trail_stop']
                else:
                    if current_price <= position['trail_activation_price']:
                        position['trailing_active'] = True
                        position['trail_stop'] = current_price * (1 + self.trail_stop_gap_pct/100)
                        position['last_trail_price'] = current_price
                        self.save_state()
                        return True, position['trail_stop']
            else:
                # Update existing trailing stop
                if is_long:
                    if current_price > position['last_trail_price'] * (1 + self.trail_increment_pct/100):
                        new_trail_stop = position['trail_stop'] * (1 + self.trail_increment_pct/100)                        
                        position['trail_stop'] = new_trail_stop
                        position['last_trail_price'] = current_price
                        self.save_state()
                        return True, position['trail_stop']
                else:
                    if current_price < position['last_trail_price'] * (1 - self.trail_increment_pct/100):
                        new_trail_stop = position['trail_stop'] * (1 - self.trail_increment_pct/100)                        
                        position['trail_stop'] = new_trail_stop
                        position['last_trail_price'] = current_price
                        self.save_state()
                        return True, position['trail_stop']            
            
            return False, None
    
    def check_exit_conditions(self, symbol, current_price):
        """Check if position should be exited"""
        with self.lock:
            if symbol not in self.open_positions:
                return False, None
            
            position = self.open_positions[symbol]
            is_long = position['direction'] == 'LONG'
            
            # Check trailing stop
            if position['trailing_active'] and position['trail_stop']:
                if is_long:
                    if current_price <= position['trail_stop']:
                        return True, "TRAIL_STOP"
                else:
                    if current_price >= position['trail_stop']:
                        return True, "TRAIL_STOP"

            # Check target profit
            if is_long:
                if current_price >= position['tp_price']:
                    return True, "TP"
            else:
                if current_price <= position['tp_price']:
                    return True, "TP"
            
            # Check time-based exit (end of day)
            current_time = datetime.now(IST).time()
            if current_time >= self.trading_end:  # 3:10 PM IST
                return True, "EOD"
            
            return False, None

class DatabaseManager:
    def __init__(self, dbconfig):
        self.dbname = dbconfig['dbname']        
        self.user = dbconfig['user']
        self.password = dbconfig['password']
        self.host = dbconfig['host']
        self.port = dbconfig['port']
        self.admin_conn = None
        self.app_conn = None
        self.logger = logging.getLogger(f"DatabaseManager")

        colored_log(self.logger, 'info', f"Initializing TimescaleDB connection to {self.host}:{self.port} as user '{self.user}' for database '{self.dbname}'", success=True)


    def _get_admin_connection(self):
        """Connection without specifying database (for admin operations)"""
        try:
            conn = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname='postgres'  # Connect to default admin DB
            )
            # Set autocommit mode for DDL operations like CREATE DATABASE
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            return conn
        except psycopg2.Error as e:
            colored_log(self.logger, 'error', f"Failed to connect to PostgreSQL server: {e}", success=False)
            raise

    def _database_exists(self, dbname):
        """Check if database exists"""
        try:
            with self._get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM pg_database WHERE datname = %s",
                        (dbname,)
                    )
                    return cursor.fetchone() is not None
        except Exception as e:
            colored_log(self.logger, 'error', f"Error checking database existence: {e}", success=False)
            return False
    

    def _create_database(self, dbname):
        """Create new database with TimescaleDB extension"""
        try:
            colored_log(self.logger, 'info', f"Creating database '{dbname}'...", success=True)
            
            # Create database with autocommit connection
            conn = self._get_admin_connection()
            try:
                with conn.cursor() as cursor:
                    # Create database
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {}").format(
                            sql.Identifier(dbname)
                        )
                    )
                    colored_log(self.logger, 'info', f"Database '{dbname}' created successfully", success=True)
            finally:
                conn.close()
                    
            # Connect to new database to install extensions
            colored_log(self.logger, 'info', "Connecting to new database...", success=True)
            conn_newdb = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname=dbname
            )
            try:
                with conn_newdb.cursor() as cursor_new:
                    cursor_new.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                    conn_newdb.commit()
                    colored_log(self.logger, 'info', "Database connected successfully", success=True)
            finally:
                conn_newdb.close()
                    
            colored_log(self.logger, 'info', f"Created database {dbname} with Database manager", success=True)
            return True
            
        except psycopg2.Error as e:
            colored_log(self.logger, 'error', f"PostgreSQL error creating database: {e}", success=False)
            return False
        except Exception as e:
            colored_log(self.logger, 'error', f"Error creating database: {e}", success=False)
            return False
    

    def _create_tables(self, dbname):
        """Create required tables and hypertables"""
        commands = [
            """
            CREATE TABLE IF NOT EXISTS ticks (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS ohlc_1m (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ohlc_1m', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS ohlc_5m (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                atr_10 DECIMAL(18, 2),
                volume_10 BIGINT,                
                nifty_trend_15m INT,
                curbot DECIMAL(18, 2),
                curtop DECIMAL(18, 2),
                cum_intraday_volume BIGINT,
                strategy_8 BOOLEAN,
                strategy_9 BOOLEAN,
                strategy_10 BOOLEAN,
                strategy_11 BOOLEAN,
                strategy_12 BOOLEAN,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ohlc_5m', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS ohlc_15m (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                atr_10 DECIMAL(18, 2),
                volume_10 BIGINT,
                nifty_trend_15m INT,
                curbot DECIMAL(18, 2),
                curtop DECIMAL(18, 2),
                cum_intraday_volume BIGINT,
                strategy_8 BOOLEAN,
                strategy_9 BOOLEAN,
                strategy_10 BOOLEAN,
                strategy_11 BOOLEAN,
                strategy_12 BOOLEAN,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ohlc_15m', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS ohlc_1h (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ohlc_1h', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS ohlc_D (
                time TIMESTAMPTZ NOT NULL,
                symbol VARCHAR(20) NOT NULL,                
                open DECIMAL(18, 2),
                high DECIMAL(18, 2),
                low DECIMAL(18, 2),
                close DECIMAL(18, 2),
                volume BIGINT,
                PRIMARY KEY (time, symbol)
            )
            """,
            """
            SELECT create_hypertable('ohlc_D', 'time', if_not_exists => TRUE)
            """,
            """
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                strategy VARCHAR(10) NOT NULL,
                quantity INTEGER NOT NULL,
                entry_time TIMESTAMPTZ NOT NULL,
                entry_price DECIMAL(18, 2) NOT NULL,
                exit_time TIMESTAMPTZ NOT NULL,
                exit_price DECIMAL(18, 2) NOT NULL,
                exit_reason VARCHAR(20) NOT NULL,
                gross_pnl DECIMAL(18, 2) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                capital_used DECIMAL(18, 2) NOT NULL,
                tax DECIMAL(18, 2) NOT NULL,
                brokerage DECIMAL(18, 2) NOT NULL,
                net_pnl DECIMAL(18, 2) NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time ON ticks (time, symbol)""",
            """CREATE INDEX IF NOT EXISTS idx_ohlc_1m_symbol_time ON ohlc_1m (time, symbol)""",
            """CREATE INDEX IF NOT EXISTS idx_ohlc_5m_symbol_time ON ohlc_5m (time, symbol)""",
            """CREATE INDEX IF NOT EXISTS idx_ohlc_15m_symbol_time ON ohlc_15m (time, symbol)""",
            """CREATE INDEX IF NOT EXISTS idx_ohlc_1h_symbol_time ON ohlc_1h (time, symbol)""",
            """CREATE INDEX IF NOT EXISTS idx_ohlc_d_symbol_time ON ohlc_D (time, symbol)"""
        ]
        
        try:
            conn = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname=dbname
            )
            try:
                with conn.cursor() as cursor:
                    for i, command in enumerate(commands):
                        try:
                            cursor.execute(command)
                            colored_log(self.logger, 'debug', f"Executed command {i+1}/{len(commands)}", success=True)
                        except psycopg2.Error as e:
                            # Skip hypertable creation if table already exists as hypertable
                            if "already a hypertable" in str(e):
                                colored_log(self.logger, 'info', f"Table already exists as hypertable, skipping: {e}", success=True)
                                continue
                            else:
                                colored_log(self.logger, 'error', f"Error executing command {i+1}: {e}", success=False)
                                colored_log(self.logger, 'error', f"Command was: {command}", success=False)
                                raise
                conn.commit()
                colored_log(self.logger, 'info', "Created tables and hypertables successfully", success=True)
            finally:
                conn.close()
                
        except psycopg2.Error as e:
            colored_log(self.logger, 'error', f"PostgreSQL error creating tables: {e}", success=False)
            raise
        except Exception as e:
            colored_log(self.logger, 'error', f"Error creating tables: {e}", success=False)
            raise
    
    def test_connection(self):
        """Test database connection"""
        try:
            conn = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname='postgres'  # Test with default database first
            )
            conn.close()
            colored_log(self.logger, 'info', "Database connection test successful", success=True)
            return True
        except psycopg2.Error as e:
            colored_log(self.logger, 'error', f"Database connection test failed: {e}", success=False)
            return False

    def initialize_database(self, dbname):
        """Main initialization method"""
        # Test connection first
        if not self.test_connection():
            raise RuntimeError("Cannot connect to PostgreSQL server. Check your connection parameters.")
        
        if not self._database_exists(dbname):
            colored_log(self.logger, 'info', f"Database {dbname} not found, creating...", success=True)
            if not self._create_database(dbname):
                raise RuntimeError("Failed to create database")
        else:
            colored_log(self.logger, 'info', f"Database {dbname} already exists", success=True)
        
        self._create_tables(dbname)
        
        # Return an application connection
        try:
            app_conn = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname=dbname
            )
            colored_log(self.logger, 'info', "Database connection established successfully", success=True)
            return app_conn
        
        except psycopg2.Error as e:
            colored_log(self.logger, 'error', f"Database connection failed: {e}", success=False)
            raise
        except Exception as e:
            colored_log(self.logger, 'error', f"Database connection failed: {e}", success=False)
            raise

    def clean_database(self, dbname):
        """Clear all records from all tables in the database"""
        try:
            colored_log(self.logger, 'info', "Cleaning database tables...", success=True)
            tables = ['ticks', 'ohlc_1m', 'ohlc_5m', 'ohlc_15m', 'ohlc_D']  # Add all your table names here

            conn = psycopg2.connect(
                user=self.user,
                password=self.password,
                host=self.host,
                port=self.port,
                dbname=dbname
            )
            
            with conn.cursor() as cursor:
                # Disable triggers temporarily to avoid hypertable constraints
                cursor.execute("SET session_replication_role = 'replica';")
                
                for table in tables:
                    try:
                        cursor.execute(f"TRUNCATE TABLE {table} CASCADE;")
                        colored_log(self.logger, 'info', f"Cleared table: {table}", success=True)
                    except Exception as e:
                        colored_log(self.logger, 'error', f"Error clearing table {table}: {e}", success=False)
                        conn.rollback()
                        continue
                
                # Re-enable triggers
                cursor.execute("SET session_replication_role = 'origin';")
                conn.commit()
                
            colored_log(self.logger, 'info', "Database cleaning completed successfully", success=True)
            return True
        except Exception as e:
            colored_log(self.logger, 'error', f"Database cleaning failed: {e}", success=False)
            conn.rollback()
            return False


class HeartbeatMonitor:
    """Monitors real-time data continuity"""
    def __init__(self, db_conn, logger, symbols, api_client, market_start, get_active_symbols_callback=None):
        self.db_conn = db_conn
        self.logger = logger
        self.last_check_time = datetime.now(UTC)
        self.missing_data_alerts = set()
        self.alert_cooldown = timedelta(minutes=5)  # Don't spam alerts
        self.symbols = symbols
        self.api_client = api_client
        self.market_start = market_start  # Market start time
        self.get_active_symbols = get_active_symbols_callback  # Callback to get symbols with active positions

    def check_data_continuity(self, data_manager=None):
        """Check if OHLC data is arriving continuously for all timeframes with recovery option"""
        overall_status = True  # Initialize status at the start
        current_time = datetime.now(IST)  # Changed from UTC to IST to match market time
        
        # Only check every 60 seconds
        if (current_time - self.last_check_time).total_seconds() < 120:
            return overall_status
            
        self.last_check_time = current_time
        
        try:
            # Configuration for different timeframes
            timeframe_configs = {
                '1m': {
                    'lookback_minutes': 5,
                    'interval': '1 minute',
                    'table': 'ohlc_1m',
                    'min_data_points': 5  # Expected data points in lookback period
                },
                '5m': {
                    'lookback_minutes': 30,
                    'interval': '5 minutes',
                    'table': 'ohlc_5m',
                    'min_data_points': 6  # Expected data points in lookback period
                },
                '15m': {
                    'lookback_minutes': 120,
                    'interval': '15 minutes',
                    'table': 'ohlc_15m',
                    'min_data_points': 8  # Expected data points in lookback period
                }
            }
            
            overall_status = True  # Track if all timeframes are healthy
            
            # Get market start time for today - ensure same timezone as current_time
            market_start_time = current_time.replace(hour=self.market_start.hour, minute=self.market_start.minute, second=0, microsecond=0)
            
            for timeframe, config in timeframe_configs.items():
                # Get symbols to check based on timeframe
                if timeframe == '1m':
                    # For 1m timeframe, only check symbols with active positions (used for exit signals)
                    symbols_to_check = self.get_active_symbols() if self.get_active_symbols else []
                    if not symbols_to_check:
                        colored_log(self.logger, 'debug', f"No active positions - skipping 1m data continuity check", success=True)
                        continue
                else:
                    # For 5m and 15m timeframes, check all symbols (used for entry signals)
                    symbols_to_check = self.symbols
                # Calculate time range for this timeframe
                # First, get the current time and align it to the previous completed interval
                current_aligned = current_time.replace(second=0, microsecond=0)
                
                # For 1m: align to the previous minute
                if timeframe == '1m':
                    end_time = current_aligned - timedelta(minutes=1)
                else:
                    # For 5m and 15m: align to the previous completed interval
                    interval_minutes = int(timeframe[:-1])
                    minutes_to_align = current_aligned.minute % interval_minutes
                    if minutes_to_align == 0:
                        # If we're exactly at an interval boundary, go back one full interval
                        end_time = current_aligned - timedelta(minutes=2 * interval_minutes)
                    else:
                        # Otherwise, align to the previous interval
                        end_time = current_aligned - timedelta(minutes=minutes_to_align+interval_minutes)
                
                # Calculate start time but don't go before market open
                start_time = end_time - timedelta(minutes=config['lookback_minutes'])
                if start_time < market_start_time:
                    start_time = market_start_time
                
                # Ensure both times are properly aligned to interval boundaries for proper generate_series
                if timeframe != '1m':
                    interval_minutes = int(timeframe[:-1])
                    
                    # Force align start_time to interval boundary (important for generate_series)
                    start_minutes = start_time.minute % interval_minutes
                    if start_minutes != 0:
                        # Preserve timezone while aligning
                        start_time = start_time.replace(minute=start_time.minute - start_minutes, second=0, microsecond=0)
                    
                    # Force align end_time to interval boundary
                    end_minutes = end_time.minute % interval_minutes  
                    if end_minutes != 0:
                        # Preserve timezone while aligning
                        end_time = end_time.replace(minute=end_time.minute - end_minutes, second=0, microsecond=0)
                
                # Final check: Ensure both times have consistent timezone representation
                if start_time.tzinfo != end_time.tzinfo or start_time.strftime('%z') != end_time.strftime('%z'):
                    colored_log(self.logger, 'warning', 
                              f"Timezone mismatch detected for {timeframe}: start_tz={start_time.strftime('%z')}, end_tz={end_time.strftime('%z')}", 
                              success=False)
                    # Force both to have same timezone as current_time
                    start_time = start_time.replace(tzinfo=current_time.tzinfo)
                    end_time = end_time.replace(tzinfo=current_time.tzinfo)
                
                # Critical fix: Ensure start_time is always before end_time
                if start_time >= end_time:
                    colored_log(self.logger, 'warning', 
                              f"Invalid time range for {timeframe}: start_time ({start_time}) >= end_time ({end_time}). Skipping check.", 
                              success=False)
                    continue
                
                # Debug: Show the time range being checked
                colored_log(self.logger, 'debug', 
                          f"Checking {timeframe} data from {start_time.strftime('%H:%M:%S')} to {end_time.strftime('%H:%M:%S')} (current time: {current_time.strftime('%H:%M:%S')})", 
                          success=True)
                
                # Debug: Show timezone info to catch timezone inconsistencies  
                colored_log(self.logger, 'debug', 
                          f"Timezone check - start: {start_time.strftime('%H:%M:%S %z')}, end: {end_time.strftime('%H:%M:%S %z')}", 
                          success=True)               
                
                # # Debug: Check what's in the table
                # debug_query = f"SELECT COUNT(*) as count FROM {config['table']}"
                # debug_result = pd.read_sql(debug_query, self.db_conn)
                # table_count = debug_result.iloc[0]['count']
                # colored_log(self.logger, 'info', f"Table {config['table']} has {table_count} total records", success=True)
                
                # if table_count == 0:
                #     colored_log(self.logger, 'warning', f"No data found in {config['table']} table", success=False)
                #     overall_status = False
                #     continue
                
                # # Check for missing intervals AND missing symbols
                # First, let's see what's actually in the table
                # simple_check_query = f"SELECT COUNT(*) as total_records, MIN(time) as earliest_time, MAX(time) as latest_time FROM {config['table']}"
                # simple_result = pd.read_sql(simple_check_query, self.db_conn)
                # if not simple_result.empty:
                #     total_records = simple_result.iloc[0]['total_records']
                #     earliest_time = simple_result.iloc[0]['earliest_time']
                #     latest_time = simple_result.iloc[0]['latest_time']
                #     colored_log(self.logger, 'info', 
                #               f"Table {config['table']} has {total_records} records, time range: {earliest_time} to {latest_time}", 
                #               success=True)
                
                query = f"""
                    WITH expected_intervals AS (
                        SELECT generate_series(%s, %s, interval '{config['interval']}') as expected_time
                    ),
                    expected_count AS (
                        SELECT COUNT(*) as total_intervals 
                        FROM expected_intervals
                    ),
                    expected_symbols AS (
                        SELECT unnest(%s::text[]) as expected_symbol
                    ),
                    actual_data AS (
                        SELECT time, symbol 
                        FROM {config['table']}
                        WHERE time >= %s AND time <= %s
                    ),
                    interval_coverage AS (
                        SELECT e.expected_time, 
                            COUNT(a.time) as data_count,
                            COUNT(DISTINCT a.symbol) as symbols_count,
                            array_agg(DISTINCT a.symbol) as present_symbols
                        FROM expected_intervals e
                        LEFT JOIN actual_data a ON e.expected_time = a.time
                        GROUP BY e.expected_time
                    ),
                    symbol_coverage AS (
                        SELECT es.expected_symbol,
                            COUNT(a.time) as data_count,
                            array_agg(DISTINCT a.time) as present_times
                        FROM expected_symbols es
                        LEFT JOIN actual_data a ON es.expected_symbol = a.symbol 
                            AND a.time >= %s AND a.time <= %s
                        GROUP BY es.expected_symbol
                    )
                    SELECT 
                        -- Interval coverage results
                        (SELECT json_agg(row_to_json(ic)) FROM interval_coverage ic) as interval_results,
                        -- Symbol coverage results  
                        (SELECT json_agg(row_to_json(sc)) FROM symbol_coverage sc) as symbol_results,
                        -- Overall stats
                        (SELECT COUNT(*) FROM interval_coverage WHERE data_count = 0) as missing_intervals_count,
                        -- Count symbols with missing intervals
                        (SELECT COUNT(*) 
                         FROM symbol_coverage sc, expected_count ec
                         WHERE sc.data_count < ec.total_intervals) as incomplete_symbols_count,
                        -- List symbols with missing intervals
                        (SELECT array_agg(expected_symbol) 
                         FROM symbol_coverage sc, expected_count ec
                         WHERE sc.data_count < ec.total_intervals) as incomplete_symbols
                """
            
                # Pass symbols as parameter (convert to PostgreSQL array format)
                symbols_param = "{" + ",".join(symbols_to_check) + "}"
                
                # Add debug logging for query parameters
                colored_log(self.logger, 'debug', 
                          f"Data continuity check for {timeframe}: start_time={start_time}, end_time={end_time}, symbols={symbols_to_check}", 
                          success=True)
                
                # # Debug: Check what data is in the expected time range
                # range_check_query = f"SELECT symbol, COUNT(*) as count FROM {config['table']} WHERE time >= %s AND time <= %s GROUP BY symbol ORDER BY symbol"
                # range_result = pd.read_sql(range_check_query, self.db_conn, params=(start_time, end_time))
                
                # if not range_result.empty:
                #     colored_log(self.logger, 'info', f"Records per symbol in time range {start_time} to {end_time}:", success=True)
                #     for _, row in range_result.iterrows():
                #         colored_log(self.logger, 'info', f"  {row['symbol']}: {row['count']} records", success=True)
                # else:
                #     colored_log(self.logger, 'warning', f"No records found in time range {start_time} to {end_time}", success=False)
                
                result = pd.read_sql(query, self.db_conn, 
                                params=(start_time, end_time, symbols_param, 
                                        start_time, end_time, start_time, end_time))
                
                # Add debug logging for query results
                colored_log(self.logger, 'debug', 
                          f"Query result for {timeframe}: empty={result.empty}, shape={result.shape if not result.empty else 'N/A'}", 
                          success=True)
                
                if result.empty:
                    colored_log(self.logger, 'warning', f"No {timeframe} data found in the last {config['lookback_minutes']} minutes", success=False)
                    overall_status = False
                    continue
                
                # Extract results
                interval_results = result.iloc[0]['interval_results']
                symbol_results = result.iloc[0]['symbol_results']
                missing_intervals_count = result.iloc[0]['missing_intervals_count']
                incomplete_symbols_count = result.iloc[0]['incomplete_symbols_count']
                incomplete_symbols = result.iloc[0]['incomplete_symbols'] or []
                
                # Convert JSON results to DataFrames
                interval_df = pd.DataFrame(interval_results) if interval_results else pd.DataFrame()
                symbol_df = pd.DataFrame(symbol_results) if symbol_results else pd.DataFrame()
                
                current_alert_key = f"{timeframe}_{start_time}_{end_time}"
                
                # Check for missing intervals
                if missing_intervals_count > 0 and interval_df is not None:
                    missing_intervals = interval_df[interval_df['data_count'] == 0]
                    if not missing_intervals.empty:
                        missing_times = pd.to_datetime(missing_intervals['expected_time']).dt.strftime('%H:%M:%S').tolist()
                        alert_msg = f"MISSING {timeframe} INTERVALS: No data at {', '.join(missing_times)}"
                        
                        if current_alert_key not in self.missing_data_alerts:
                            colored_log(self.logger, 'error', alert_msg, success=False)
                            self.missing_data_alerts.add(current_alert_key)
                            threading.Timer(self.alert_cooldown.total_seconds(), 
                                        lambda: self.missing_data_alerts.discard(current_alert_key)).start()
                        overall_status = False

                # Check for incomplete symbols (missing data in some intervals)
                if incomplete_symbols_count > 0:
                    incomplete_symbols_list = incomplete_symbols if isinstance(incomplete_symbols, list) else list(incomplete_symbols)
                    alert_msg = f"INCOMPLETE SYMBOLS ({timeframe}): {incomplete_symbols_list} have missing data"
                    
                    if current_alert_key not in self.missing_data_alerts:
                        colored_log(self.logger, 'warning', alert_msg)
                        self.missing_data_alerts.add(current_alert_key)
                        threading.Timer(self.alert_cooldown.total_seconds(), 
                                    lambda: self.missing_data_alerts.discard(current_alert_key)).start()
                    overall_status = False
                
                # Check for completely missing symbols (not in database at all)
                if symbol_df is not None and not symbol_df.empty:
                    all_symbols_in_db = symbol_df['expected_symbol'].unique().tolist()
                    completely_missing_symbols = [s for s in symbols_to_check if s not in all_symbols_in_db]
                    
                    if completely_missing_symbols:
                        alert_msg = f"COMPLETELY MISSING SYMBOLS ({timeframe}): {completely_missing_symbols}"
                        colored_log(self.logger, 'error', alert_msg, success=False)
                        overall_status = False
                
                # Trigger emergency recovery if needed
                if data_manager:
                    # Recovery for missing intervals (serious data gap)
                    if missing_intervals_count > 2:
                        colored_log(self.logger, 'error', f"Attempting {timeframe} data recovery for missing intervals", success=False)
                        data_manager.emergency_data_recovery(timeframe=timeframe)
                        # Don't continue, let the status be marked as unhealthy
                    
                    # Recovery for incomplete symbols
                    if incomplete_symbols:
                        colored_log(self.logger, 'warning', f"Attempting {timeframe} data recovery for incomplete symbols: {incomplete_symbols}", success=False)
                        data_manager.emergency_data_recovery(timeframe=timeframe, specific_symbols=incomplete_symbols)
                        # Don't continue, let the status be marked as unhealthy
                
                # Log status for this timeframe
                if missing_intervals_count == 0 and incomplete_symbols_count == 0:
                    colored_log(self.logger, 'info', 
                              f"DATA OK ({timeframe}): All {len(symbols_to_check)} symbols have complete data for last {config['lookback_minutes']} minutes", 
                              success=True)
                else:
                    colored_log(self.logger, 'warning', 
                              f"DATA ISSUES ({timeframe}): Missing intervals: {missing_intervals_count}, Incomplete symbols: {incomplete_symbols_count}", 
                              success=False)
                    # Ensure overall status reflects the issues
                    overall_status = False
            
            return overall_status
                
        except Exception as e:
            colored_log(self.logger, 'error', f"Heartbeat check failed: {e}", success=False)
            import traceback
            colored_log(self.logger, 'error', traceback.format_exc(), success=False)
            overall_status = False
            return overall_status    
    

def colored_log(logger, level, message, success=None):
    """Helper function for colored logging
    level: 'info', 'warning', 'error', 'debug'
    success: True for success messages (green), False for errors (red), None for regular info (yellow)
    """
    if success is True:
        colored_msg = f"{Fore.GREEN}{message}{Style.RESET_ALL}"
    elif success is False:
        colored_msg = f"{Fore.RED}{message}{Style.RESET_ALL}"
    else:
        colored_msg = f"{Fore.YELLOW}{message}{Style.RESET_ALL}"
        
    if level == 'info':
        logger.info(colored_msg)
    elif level == 'warning':
        logger.warning(colored_msg)
    elif level == 'error':
        logger.error(colored_msg)
    elif level == 'debug':
        logger.debug(colored_msg)

class LiveDataManager:
    """Manages real-time data and indicator calculations"""
    def __init__(self, db_config, api_client, api_key, symbols, market_start, market_end, get_active_symbols_callback=None):
        self.db_manager = DatabaseManager(db_config)
        self.db_conn = self.db_manager.initialize_database(db_config['dbname'])        
        self.symbols = symbols
        self.instruments_list = [{"exchange": "NSE", "symbol": symbol} for symbol in symbols]
        self.instruments_list.append({"exchange": "NSE_INDEX", "symbol": "NIFTY"})
        self.api_client = api_client
        self.api_key = api_key
        self.market_start = market_start
        self.market_end = market_end
        self.get_active_symbols = get_active_symbols_callback  # Callback to get symbols with active positions
        self.interrupt_flag = False  # Add interrupt flag
        self.lock = threading.RLock()
        self.logger = logging.getLogger("LiveDataManager")
        colored_log(self.logger, 'info', f'Symbols: {self.symbols}', success=True)
        colored_log(self.logger, 'info', f'Instruments list: {self.instruments_list}', success=True)

        # Initialize Kafka Consumer
        self.consumer = KafkaConsumer(
            'tick_data',
            bootstrap_servers='localhost:9092',
            group_id='tick-processor',
            auto_offset_reset='earliest'
            #key_deserializer=lambda k: k.decode('utf-8') if k else None,
            #value_deserializer=lambda v: json.loads(v.decode('utf-8'))
        )

        colored_log(self.logger, 'info', "Starting consumer with configuration:", success=True)
        colored_log(self.logger, 'info', f"Group ID: {self.consumer.config['group_id']}", success=True)
        colored_log(self.logger, 'info', f"Brokers: {self.consumer.config['bootstrap_servers']}", success=True)

        self.reset_aggregation_buffers()               
        
        #self.db_manager.clean_database(db_config['dbname'])   

        # Check if the database has last 20 days of data for all symbols. If not load the data.
        symbols_needing_data = self.check_historical_data_loaded(20, self.symbols, ['5m', '15m', 'D'])

        if symbols_needing_data:
            # Initialize with historical data only for symbols that need it
            colored_log(self.logger, 'info', f"Loading historical data for {len(symbols_needing_data)} symbols: {symbols_needing_data}", success=True)
            self._load_historical_data(20, symbols_needing_data, ['5m', '15m', 'D'])
        else:
            colored_log(self.logger, 'info', "All symbols have complete historical data - skipping data loading", success=True)   
        
        # Connect and subscribe to real-time data
        self.api_client.connect()
        self.api_client.subscribe_quote(self.instruments_list, on_data_received=self.on_data_received)

        # Initialize heartbeat monitor
        self.heartbeat_monitor = HeartbeatMonitor(
            db_conn=self.db_conn,
            logger=self.logger,
            symbols=self.symbols,
            api_client=self.api_client,
            market_start=self.market_start,
            get_active_symbols_callback=self.get_active_symbols
        )

        self.is_running = False
        self.data_thread = None  

    
    def check_historical_data_loaded(self, days, symbols, intervals):
        """Check if the database has last N days of data for all symbols across all intervals. 
        Returns a list of symbols that need data loading, or empty list if all symbols have complete data."""
        try:
            current_date = datetime.now(IST).date()
            start_date = current_date - timedelta(days=days)
            
            colored_log(self.logger, 'info', f"Checking historical data for {len(symbols)} symbols across {len(intervals)} intervals for last {days} days", success=True)
            
            symbols_needing_data = set()  # Use set to avoid duplicates
            
            # Check each interval (timeframe)
            for interval in intervals:
                table_name = f"ohlc_{interval}"
                colored_log(self.logger, 'debug', f"Checking table {table_name} for {len(symbols)} symbols", success=True)
                
                # Check each symbol
                for symbol in symbols:
                    # Query to check if symbol has data in the required date range
                    check_query = f"""
                        SELECT COUNT(*) as record_count,
                               MIN(DATE(time)) as earliest_date,
                               MAX(DATE(time)) as latest_date
                        FROM {table_name}
                        WHERE symbol = %s 
                        AND DATE(time) >= %s 
                        AND DATE(time) <= %s
                    """
                    
                    result = pd.read_sql(check_query, self.db_conn, params=(symbol, start_date, current_date))
                    
                    needs_data = False
                    
                    if result.empty:
                        colored_log(self.logger, 'warning', f"No data found for {symbol} in {table_name}", success=False)
                        needs_data = True
                    else:
                        record_count = result.iloc[0]['record_count']
                        earliest_date = result.iloc[0]['earliest_date']
                        latest_date = result.iloc[0]['latest_date']
                        
                        if record_count == 0:
                            colored_log(self.logger, 'warning', f"No records found for {symbol} in {table_name} for date range {start_date} to {current_date}", success=False)
                            needs_data = True
                        
                        # Check if we have recent data (at least within last 2 days)
                        elif latest_date is None or (current_date - latest_date).days > 2:
                            colored_log(self.logger, 'warning', f"Latest data for {symbol} in {table_name} is from {latest_date}, which is too old", success=False)
                            needs_data = True

                        elif earliest_date is None or (current_date - earliest_date).days < (days - 3):
                            colored_log(self.logger, 'warning', f"Earliest data for {symbol} in {table_name} is from {earliest_date}, which is not enough for {days} days", success=False)
                            needs_data = True
                        
                        else:
                            colored_log(self.logger, 'debug', f"{symbol} in {table_name}: {record_count} records, date range: {earliest_date} to {latest_date}", success=True)
                    
                    if needs_data:
                        symbols_needing_data.add(symbol)
            
            symbols_needing_data_list = list(symbols_needing_data)
            
            if not symbols_needing_data_list:
                colored_log(self.logger, 'info', f"Historical data check PASSED: All {len(symbols)} symbols have complete data across all {len(intervals)} intervals", success=True)
            else:
                colored_log(self.logger, 'warning', f"Historical data check FAILED: {len(symbols_needing_data_list)} symbols need data loading: {symbols_needing_data_list}", success=False)
            
            return symbols_needing_data_list
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error checking historical data loaded: {e}", success=False)
            import traceback
            colored_log(self.logger, 'error', traceback.format_exc(), success=False)
            return symbols  # Return all symbols if error occurs, to be safe


    def emergency_data_recovery(self, timeframe=None, specific_symbols=None):
        """
        Attempt to recover from data gaps
        Args:
            timeframe: Specific timeframe to recover ('1m', '5m', '15m'). If None, recovers all timeframes.
            specific_symbols: List of specific symbols to recover. If None, recovers all symbols.
        """
        colored_log(self.logger, 'debug', "ENTERED EMERGENCY DATA RECOVERY...", success=True)
        try:
            # Use specific symbols if provided, otherwise all symbols
            symbols_to_recover = specific_symbols if specific_symbols else self.symbols
            
            # Determine which timeframes to recover
            timeframes_to_recover = [timeframe] if timeframe else ['1m', '5m', '15m']
            
            colored_log(self.logger, 'warning', 
                       f"Attempting data recovery for timeframe(s): {timeframes_to_recover}, symbols: {symbols_to_recover}", 
                       success=False)
            
            #self._load_historical_data(1, symbols_to_recover, timeframes_to_recover, 'recovery')     

            current_time = datetime.now(IST)
            current_date = current_time.date()
            start_time = current_time.replace(hour=9, minute=15, second=0, microsecond=0).strftime('%H:%M:%S')            
            date_str = current_date.strftime("%Y-%m-%d")
            current_aligned = current_time.replace(second=0, microsecond=0)

            for symbol in symbols_to_recover:
                for timeframe in timeframes_to_recover:
                    if timeframe == '1m':
                        end_time = (current_aligned - timedelta(minutes=1)).strftime('%H:%M:%S')
                    else:
                        interval_minutes = int(timeframe[:-1])
                        minutes_to_align = current_aligned.minute % interval_minutes
                        if minutes_to_align == 0:
                            end_time = (current_aligned - timedelta(minutes=interval_minutes)).strftime('%H:%M:%S')
                        else:
                            end_time = (current_aligned - timedelta(minutes=minutes_to_align)).strftime('%H:%M:%S')

                    self.fetch_intraday_data(symbol, timeframe, self.api_client, date_str, date_str, start_time, end_time, 'recovery')     

            colored_log(self.logger, 'info', "EMERGENCY DATA RECOVERY COMPLETED!", success=True)                                                 
        
        except Exception as e:
            colored_log(self.logger, 'error', f"Emergency recovery failed: {e}", success=False)
            

    def reset_aggregation_buffers(self):
        """Initialize/reset aggregation buffers"""
        with self.lock:
            self.tick_buffer = {
                '1m': {},
                '5m': {},
                '15m': {}
            }
            now = datetime.now(pytz.utc)
            self.last_agg_time = {
                '1m': self.floor_to_interval(now, 1),
                '5m': self.floor_to_interval(now, 5),
                '15m': self.floor_to_interval(now, 15)
            }
            self.aggregation_state = {
                '1m': {},
                '5m': {},
                '15m': {}
            }
            
            # Reset volume tracking
            self.last_period_volume = {
                '1m': {},
                '5m': {},
                '15m': {}
            }   
    
    def on_new_candle(self, symbol, timeframe, candle):
        """Callback method when a new candle is formed
        This is called by MarketDataProcessor when a complete candle is formed"""
        try:
            if (timeframe == '5m' and symbol != 'NIFTY') or timeframe == '15m':
                # Only recalculate indicators for completed candles
                current_time = datetime.now()
                start_date = current_time.date() - timedelta(days=20)  # Last 20 days for indicators
                end_date = current_time.date()
                
                # Calculate indicators for the symbol
                self._calculate_and_store_indicators(symbol, start_date, end_date, timeframe)
                colored_log(self.logger, 'debug', f"Recalculated indicators for {symbol} after new {timeframe} candle", success=True)
                
        except Exception as e:
            colored_log(self.logger, 'error', f"Error handling new candle for {symbol} {timeframe}: {e}", success=False)    
    

    def floor_to_interval(self, dt, minutes=1):
        """Floor a datetime to the start of its minute/5m/15m interval"""
        discard = timedelta(
            minutes=dt.minute % minutes,
            seconds=dt.second,
            microseconds=dt.microsecond
        )
        return dt - discard
        
    def _load_historical_data(self, days, symbols, intervals, purpose='general'):
        """Load last 20 days data and clear existing intraday data"""
        try:                   
            
            # Calculate date range (last 20 days)
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)
            
            if purpose == 'general':
                colored_log(self.logger, 'info', f"Loading {days} days of historical data via API from {start_date} to {end_date}", success=True)                
            elif purpose == 'recovery':
                colored_log(self.logger, 'info', f"Loading {days} days of historical data via API from {start_date} to {end_date}", success=True)                
            
            # Define intervals to fetch (start with 1m as base, then aggregate)
            intervals_to_fetch = intervals

            # Create all combinations of (symbol, interval)
            symbol_interval_pairs = list(product(symbols, intervals_to_fetch))

            # Add "NIFTY" "D" in the symbol_interval_pairs
            symbol_interval_pairs.append(("NIFTY", "D")) 
            symbol_interval_pairs.append(("NIFTY", "15m"))  

            if purpose == 'recovery':
                symbol_interval_pairs.append(("NIFTY", "1m"))           
            
            # Use ThreadPool for parallel data fetching
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = []
                
                for symbol, interval in symbol_interval_pairs:
                    future = executor.submit(
                        self._fetch_symbol_historical_data,
                        symbol, 
                        interval, 
                        start_date.strftime("%Y-%m-%d"), 
                        end_date.strftime("%Y-%m-%d")
                    )
                    futures.append((future, symbol, interval))
                
                # Wait for all futures to complete
                for future, symbol, interval in futures:
                    try:
                        future.result(timeout=300)  # 5 minute timeout per symbol-interval
                        colored_log(self.logger, 'debug', f"Completed {symbol} {interval}", success=True)
                    except Exception as e:
                        colored_log(self.logger, 'error', f"Failed {symbol} {interval}: {e}", success=False)            
            
            if purpose == 'general':
                colored_log(self.logger, 'info', "Historical data loading completed!", success=True)
            elif purpose == 'recovery':
                colored_log(self.logger, 'info', "Recovery data loading completed!", success=True)
                    
        except Exception as e:
            colored_log(self.logger, 'error', f"Error in _load_historical_data: {e}", success=False)
            raise
    
    def _fetch_symbol_historical_data(self, symbol, interval, start_date, end_date):
        """Fetch historical data for a single symbol-interval using TimescaleDB functions"""
        try:
            colored_log(self.logger, 'info', f"Fetching {symbol} {interval} from {start_date} to {end_date}", success=True)
            
            # Use process_symbol_interval function
            self.process_symbol_interval(
                symbol=symbol,
                interval=interval,
                client=self.api_client,
                start_date=start_date,
                end_date=end_date,
                mode="live"
            ) 
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error fetching {symbol} {interval}: {e}", success=False)
            raise
    
    def _process_symbol_post_fetch(self, symbol, start_date, end_date):
        """Process symbol after data fetch - calculate indicators"""
        try:
            # Calculate and store indicators for all timeframes
            self._calculate_and_store_indicators(symbol, start_date, end_date, "post_fetch")
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error post-processing {symbol}: {e}", success=False)
            raise
    
    
    def _calculate_and_store_indicators(self, symbol, start_date, end_date, mode):
        """Calculate and store indicators for all timeframes - OPTIMIZED for real-time updates"""
        try:
            # Check if this is a real-time update (5m or 15m mode) - use optimized approach
            if mode in ['5m', '15m']:
                self._calculate_indicators_incremental(symbol, mode)
            else:
                # Full calculation for initial load or post_fetch mode
                self._calculate_indicators_full(symbol, start_date, end_date, mode)
                    
        except Exception as e:
            colored_log(self.logger, 'error', f"Error calculating indicators for {symbol}: {e}", success=False)

    def _calculate_indicators_incremental(self, symbol, timeframe):
        """Calculate indicators only for the latest candle - OPTIMIZED for real-time"""
        try:
            # Get minimal historical data needed for indicator calculations
            if timeframe == '15m':
                lookback_days = 20
            elif timeframe == '5m':
                lookback_days = 10
            else:
                lookback_days = 1

            end_date = datetime.now(IST).date()
            start_date = end_date - timedelta(days=lookback_days)
            
            # Fetch only necessary data
            df_current = self.fetch_lookback_data(start_date, end_date, timeframe, symbol)
            df_daily = self.fetch_lookback_data(end_date - timedelta(days=20), end_date, 'd', symbol)
            df_nifty_15m = self.fetch_lookback_data(end_date - timedelta(days=20), end_date, 'nifty_15m', 'NIFTY')
            
            if df_current.empty or df_daily.empty or df_nifty_15m.empty:
                colored_log(self.logger, 'warning', f"Missing data for incremental indicator calculation: {symbol} {timeframe}", success=False)
                return
            
            # Calculate indicators for the full dataset (needed for rolling/ewm calculations)
            df_with_indicators = self._calculate_indicators_for_timeframe(df_current, df_daily, df_nifty_15m, symbol, timeframe)
            
            if not df_with_indicators.empty:
                # Only update the database with the LATEST record (most recent candle)
                latest_record = df_with_indicators.tail(1).copy()
                self._update_indicators_in_db(latest_record, symbol, timeframe)
                colored_log(self.logger, 'debug', f"Updated indicators for latest {timeframe} candle: {symbol}", success=True)
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error in incremental indicator calculation for {symbol} {timeframe}: {e}", success=False)

    def _calculate_indicators_full(self, symbol, start_date, end_date, mode):
        """Full indicator calculation for initial loads - ORIGINAL APPROACH"""
        try:                       
            df_all_dict  = {
            '15m': self.fetch_lookback_data(end_date- timedelta(days=20), end_date, '15m', symbol),
            '5m': self.fetch_lookback_data(end_date- timedelta(days=10), end_date, '5m', symbol),
            '1m': self.fetch_lookback_data(end_date- timedelta(days=1) , end_date, '1m', symbol),
            'd': self.fetch_lookback_data(start_date - timedelta(days=20), end_date, 'd', symbol),
            'nifty_15m': self.fetch_lookback_data(start_date - timedelta(days=20), end_date, 'nifty_15m', 'NIFTY'),
            }

            # Calculate indicators
            try:
                symbol_data_with_indicators = self.calculate_all_indicators_once(df_all_dict, symbol)
            except Exception as e:
                colored_log(self.logger, 'error', f"Error in calculate_all_indicators_once for {symbol}: {e}", success=False)
                raise
                        
            # Update database with indicators
            for timeframe, df in symbol_data_with_indicators.items():
                if mode == "post_fetch" and (timeframe == '5m' or timeframe == '15m'):
                    if not df.empty:
                        self._update_indicators_in_db(df, symbol, timeframe)
                elif (mode == '5m' or mode == '15m') and timeframe == mode:
                    if not df.empty:
                        self._update_indicators_in_db(df, symbol, timeframe)                
                    
        except Exception as e:
            colored_log(self.logger, 'error', f"Error calculating indicators for {symbol}: {e}", success=False)

    def _calculate_indicators_for_timeframe(self, df_current, df_daily, df_nifty_15m, symbol, timeframe):
        """Calculate indicators for a specific timeframe - optimized for latest candle updates"""
        try:
            # Ensure time columns are datetime
            for df in [df_current, df_daily, df_nifty_15m]:
                if not df.empty:
                    df['time'] = pd.to_datetime(df['time'])
            
            # Calculate daily indicators (needed for merging)
            if not df_daily.empty:
                df_daily['prev_close'] = df_daily['close'].shift(1)
                df_daily['tr1'] = df_daily['high'] - df_daily['low']
                df_daily['tr2'] = abs(df_daily['high'] - df_daily['prev_close'])
                df_daily['tr3'] = abs(df_daily['low'] - df_daily['prev_close'])
                df_daily['tr'] = df_daily[['tr1', 'tr2', 'tr3']].max(axis=1)
                df_daily['atr_10'] = df_daily['tr'].ewm(span=10, adjust=False).mean()
                df_daily['volume_10'] = df_daily['volume'].rolling(window=10).mean()
                df_daily['close_10'] = df_daily['close'].rolling(window=10).mean()
                df_daily['date'] = df_daily['time'].dt.date

            # Calculate nifty 15m indicators (needed for merging)
            if not df_nifty_15m.empty:
                df_nifty_15m['date'] = df_nifty_15m['time'].dt.date
                df_nifty_15m['nifty_15m_ema_50'] = df_nifty_15m['close'].ewm(span=50, adjust=False).mean()
                df_nifty_15m['nifty_15m_ema_200'] = df_nifty_15m['close'].ewm(span=200, adjust=False).mean()
                df_nifty_15m['nifty_15m_adx'] = talib.ADX(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
                df_nifty_15m['nifty_15m_+DI'] = talib.PLUS_DI(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
                df_nifty_15m['nifty_15m_-DI'] = talib.MINUS_DI(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
                df_nifty_15m['nifty_15m_MACD'], df_nifty_15m['nifty_15m_MACD_Signal'], _ = talib.MACD(df_nifty_15m['close'], fastperiod=20, slowperiod=50, signalperiod=10)
                df_nifty_15m['nifty_trend_15m'] = df_nifty_15m.apply(self.classify_trend, args=('15m',), axis=1)
                df_nifty_15m = df_nifty_15m.groupby('date').last().reset_index()
                df_nifty_15m['nifty_trend_15m'] = df_nifty_15m['nifty_trend_15m'].shift(1)
            
            # Calculate current timeframe indicators
            if not df_current.empty:
                df_current['date'] = df_current['time'].dt.date
                
                if timeframe == '5m':
                    for period in [50, 100, 200]:
                        df_current[f'ema_{period}'] = df_current['close'].ewm(span=period, adjust=False).mean()
                    # Only keep today's date before calculating single prints
                    df_current = df_current[df_current['date'] == datetime.now(IST).date()].reset_index(drop=True)
                    # Calculate range
                    df_current['range'] = df_current['high'] - df_current['low']
                    df_current['avg_range_all'] = df_current.groupby('date')['range'].expanding().mean().reset_index(level=0, drop=True)
                    try:
                        avg_ex_first_30min_5m = df_current.groupby('date').apply(self.exclude_first_30min)
                        if isinstance(avg_ex_first_30min_5m, pd.DataFrame):
                            avg_ex_first_30min_5m = avg_ex_first_30min_5m.iloc[:, 0]
                        avg_ex_first_30min_5m = avg_ex_first_30min_5m.reset_index(level=0, drop=True)
                        df_current['avg_range_ex_first_30min'] = avg_ex_first_30min_5m.reindex(df_current.index).ffill()
                    except Exception as e:
                        self.logger.warning(f"Error calculating avg_range_ex_first_30min for 5m: {e}, using avg_range_all instead")
                        df_current['avg_range_ex_first_30min'] = df_current['avg_range_all']
                    df_current['is_range_bullish'] = (
                        (df_current['range'] > 0.7 * df_current['avg_range_ex_first_30min']) & 
                        (df_current['close'] > df_current['open']) & 
                        (df_current['close'] > (((df_current['high'] - df_current['open']) * 0.5) + df_current['open']))
                    )
                    df_current['is_range_bearish'] = (
                        (df_current['range'] > 0.7 * df_current['avg_range_ex_first_30min']) & 
                        (df_current['close'] < df_current['open']) & 
                        (df_current['close'] < (((df_current['open'] - df_current['low']) * 0.5) + df_current['low']))
                    )                   
            
                if timeframe == '15m':
                    df_current = self._calculate_zlema_macd(df_current)
                    # Only keep today's date before calculating single prints
                    df_current = df_current[df_current['date'] == datetime.now(IST).date()].reset_index(drop=True)

                # Calculate single prints
                df_current['is_first_bullish_confirmed'] = False
                df_current['is_first_bearish_confirmed'] = False
                df_current['candle_count'] = df_current.groupby(df_current['date']).cumcount() + 1
                df_current['cum_high_prev'] = df_current.groupby('date')['high'].expanding().max().shift(1).reset_index(level=0, drop=True)
                df_current['cum_low_prev'] = df_current.groupby('date')['low'].expanding().min().shift(1).reset_index(level=0, drop=True)
                df_current['cum_high'] = df_current.groupby('date')['high'].expanding().max().reset_index(level=0, drop=True)
                df_current['cum_low'] = df_current.groupby('date')['low'].expanding().min().reset_index(level=0, drop=True)
                df_current['sp_confirmed_bullish'] = (
                    (df_current['close'] > df_current['cum_high_prev']) & 
                    (df_current['close'] > df_current['open']) & 
                    (df_current['candle_count'] >= 2)
                )
                df_current['sp_confirmed_bearish'] = (
                    (df_current['close'] < df_current['cum_low_prev']) & 
                    (df_current['close'] < df_current['open']) & 
                    (df_current['candle_count'] >= 2)
                )
                
                # Mark first confirmations
                bullish_conf = df_current[df_current['sp_confirmed_bullish']]
                bearish_conf = df_current[df_current['sp_confirmed_bearish']]
                first_bullish_idx = bullish_conf.groupby('date').head(1).index
                first_bearish_idx = bearish_conf.groupby('date').head(1).index
                df_current.loc[first_bullish_idx, 'is_first_bullish_confirmed'] = True
                df_current.loc[first_bearish_idx, 'is_first_bearish_confirmed'] = True
                
                # SP levels
                sp_levels_bullish = df_current[df_current['is_first_bullish_confirmed']][['date', 'close', 'cum_high_prev']]
                sp_levels_bearish = df_current[df_current['is_first_bearish_confirmed']][['date', 'close', 'cum_low_prev']]
                sp_levels_bullish['sp_high_bullish'] = sp_levels_bullish['close']
                sp_levels_bullish['sp_low_bullish'] = sp_levels_bullish['cum_high_prev']
                sp_levels_bearish['sp_high_bearish'] = sp_levels_bearish['cum_low_prev']
                sp_levels_bearish['sp_low_bearish'] = sp_levels_bearish['close']
                sp_levels_bullish.drop(['close', 'cum_high_prev'], axis=1, inplace=True)
                sp_levels_bearish.drop(['close', 'cum_low_prev'], axis=1, inplace=True)
                
                # Merge back
                df_current = df_current.merge(sp_levels_bullish, on='date', how='left')
                df_current = df_current.merge(sp_levels_bearish, on='date', how='left')
                
                # Forward fill SP levels
                df_current['sp_high_bullish'] = df_current.groupby('date')['sp_high_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
                df_current['sp_low_bullish'] = df_current.groupby('date')['sp_low_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
                df_current['sp_high_bearish'] = df_current.groupby('date')['sp_high_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
                df_current['sp_low_bearish'] = df_current.groupby('date')['sp_low_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
                
                # Set pre-confirmation values to NaN
                df_current.loc[~df_current['sp_confirmed_bullish'].cummax(), ['sp_high_bullish', 'sp_low_bullish']] = None
                df_current.loc[~df_current['sp_confirmed_bearish'].cummax(), ['sp_high_bearish', 'sp_low_bearish']] = None
                
                # Calculate SP range percentages
                df_current['sp_bullish_range_pct'] = (df_current['sp_high_bullish'] - df_current['sp_low_bullish']) / df_current['sp_low_bullish'] * 100
                df_current['sp_bearish_range_pct'] = (df_current['sp_high_bearish'] - df_current['sp_low_bearish']) / df_current['sp_low_bearish'] * 100
                df_current['cum_sp_bullish'] = df_current.groupby('date')['sp_confirmed_bullish'].cumsum()
                df_current['cum_sp_bearish'] = df_current.groupby('date')['sp_confirmed_bearish'].cumsum()

                # VOLUME AND RANGE CALCULATIONS
                df_current['cum_intraday_volume'] = df_current.groupby('date')['volume'].cumsum()
                df_current['curtop'] = df_current.groupby('date')['high'].cummax()
                df_current['curbot'] = df_current.groupby('date')['low'].cummin()
                df_current['today_range'] = df_current['curtop'] - df_current['curbot']
                df_current['today_range_pct_10'] = df_current['today_range'] / df_current['atr_10']
                df_current['volume_range_pct_10'] = (df_current['cum_intraday_volume'] / df_current['volume_10']) / df_current['today_range_pct_10']
                
                # Merge with daily data
                if not df_daily.empty:
                    daily_cols = ['date', 'atr_10', 'volume_10', 'close_10']
                    df_current = df_current.merge(df_daily[daily_cols], on='date', how='left')

                # Merge with nifty 15m data
                if not df_nifty_15m.empty:
                    df_current = df_current.merge(df_nifty_15m[['date', 'nifty_trend_15m']], on='date', how='left')
                
                # STRATEGY DEFINITIONS
                df_current['s_8'] = (
                    (df_current['time'].dt.time >= time(4, 0)) & 
                    (df_current['time'].dt.time < time(8, 15)) & 
                    (df_current['cum_sp_bullish'] >= 1) & 
                    (df_current['sp_bullish_range_pct'] > 0.8) & 
                    (df_current['sp_bullish_range_pct'] < 1.3) & 
                    (df_current['zl_macd_signal'] == -1) & 
                    (df_current['volume_range_pct_10'] > 1) &
                    (df_current['atr_10'] / df_current['close_10'] < 0.04) &
                    (df_current['nifty_trend_15m'] >= 0)
                )
                df_current['strategy_8'] = False
                first_true_idx_8 = df_current[df_current['s_8']].groupby('date').head(1).index
                df_current.loc[first_true_idx_8, 'strategy_8'] = True
                
                df_current['s_12'] = (
                    (df_current['time'].dt.time >= time(4, 0)) & 
                    (df_current['time'].dt.time < time(8, 15)) & 
                    (df_current['cum_sp_bearish'] >= 1) & 
                    (df_current['sp_bearish_range_pct'] > 1) & 
                    (df_current['zl_macd_signal'] == 1) &
                    (df_current['volume_range_pct_10'] > 0) &
                    (df_current['volume_range_pct_10'] < 0.4) &
                    (df_current['atr_10'] / df_current['close_10'] < 0.04) &
                    (df_current['nifty_trend_15m'] <= 0)
                )
                df_current['strategy_12'] = False
                first_true_idx_12 = df_current[df_current['s_12']].groupby('date').head(1).index
                df_current.loc[first_true_idx_12, 'strategy_12'] = True                
                
                # Strategy 10 & 11 (5m)
                df_current['s_10'] = (
                    (df_current['time'].dt.time >= time(3, 50)) & 
                    (df_current['time'].dt.time < time(8, 15)) & 
                    (df_current['cum_sp_bearish'] >= 1) & 
                    (df_current['sp_bearish_range_pct'] > 0.6) & 
                    (df_current['close'] < df_current['ema_50']) & 
                    (df_current['close'] < df_current['ema_100']) & 
                    (df_current['close'] < df_current['ema_200']) & 
                    (df_current['is_range_bearish']) & 
                    (df_current['volume_range_pct_10'] > 0.3) & 
                    (df_current['volume_range_pct_10'] < 0.7) & 
                    (df_current['atr_10'] / df_current['close_10'] > 0.04) &            
                    (df_current['nifty_trend_15m'] != 1)
                )
                df_current['strategy_10'] = False
                first_true_idx_10 = df_current[df_current['s_10']].groupby('date').head(1).index
                df_current.loc[first_true_idx_10, 'strategy_10'] = True
                
                df_current['s_11'] = (
                    (df_current['time'].dt.time >= time(3, 50)) & 
                    (df_current['time'].dt.time < time(8, 15)) & 
                    (df_current['cum_sp_bullish'] >= 1) & 
                    (df_current['sp_bullish_range_pct'] > 0.8) & 
                    (df_current['close'] > df_current['ema_50']) & 
                    (df_current['close'] > df_current['ema_100']) & 
                    (df_current['close'] > df_current['ema_200']) & 
                    (df_current['is_range_bullish']) & 
                    (df_current['volume_range_pct_10'] > 0) & 
                    (df_current['volume_range_pct_10'] < 0.3) &
                    (df_current['atr_10'] / df_current['close_10'] > 0.04) &
                    (df_current['nifty_trend_15m'] != -1)
                )
                df_current['strategy_11'] = False
                first_true_idx_11 = df_current[df_current['s_11']].groupby('date').head(1).index
                df_current.loc[first_true_idx_11, 'strategy_11'] = True

                df_current['s_9'] = (
                    (df_current['time'].dt.time >= time(3, 50)) & 
                    (df_current['time'].dt.time < time(8, 15)) & 
                    (df_current['cum_sp_bullish'] >= 1) & 
                    (df_current['sp_bullish_range_pct'] > 0.8) & 
                    (df_current['close'] > df_current['ema_50']) & 
                    (df_current['close'] > df_current['ema_100']) & 
                    (df_current['close'] > df_current['ema_200']) & 
                    (df_current['is_range_bullish']) & 
                    (df_current['volume_range_pct_10'] > 0.3) & 
                    (df_current['volume_range_pct_10'] < 0.6) &
                    (df_current['atr_10'] / df_current['close_10'] < 0.04) &
                    (df_current['nifty_trend_15m'] != 1)
                )
                df_current['strategy_9'] = False
                first_true_idx_9 = df_current[df_current['s_9']].groupby('date').head(1).index
                df_current.loc[first_true_idx_9, 'strategy_9'] = True

                # Clean up temporary columns
                df_current.drop(['date'], axis=1, inplace=True, errors='ignore')
            
            return df_current
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error calculating indicators for {symbol} {timeframe}: {e}", success=False)
            return df_current  # Return original dataframe on error

    def _calculate_zlema_macd(self, df):
        """Calculate Zero-Lag EMA MACD indicators"""
        try:
            if len(df) < 50:  # Need minimum data for MACD
                return df           
            
            # Calculate ZLEMA MACD
            df['fast_zlema'] = self.zero_lag_ema(df['close'], 12)
            df['slow_zlema'] = self.zero_lag_ema(df['close'], 26)
            df['zl_macd'] = df['fast_zlema'] - df['slow_zlema']
            df['zl_signal'] = df['zl_macd'].ewm(span=9, adjust=False).mean()
            df['zl_hist'] = df['zl_macd'] - df['zl_signal']
            
            # Generate MACD Signals
            df['zl_macd_signal'] = 0
            df.loc[(df['zl_macd'] > df['zl_signal']) & 
                    (df['zl_macd'].shift(1) <= df['zl_signal'].shift(1)), 'zl_macd_signal'] = 1
            df.loc[(df['zl_macd'] < df['zl_signal']) & 
                    (df['zl_macd'].shift(1) >= df['zl_signal'].shift(1)), 'zl_macd_signal'] = -1
            df.drop(['fast_zlema', 'slow_zlema', 'zl_macd', 'zl_signal', 'zl_hist'], axis=1, inplace=True)
            return df
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error calculating ZLEMA MACD: {e}", success=False)
            return df

    def fetch_lookback_data(self, start_day, end_day, interval, symbol):
        
        #colored_log(self.logger, 'info', f"Fetching lookback data from {start_day} to {end_day}", success=True)
        
        # Get symbols with active positions for 1m data (used for exit signals)
        active_symbols = self.get_active_symbols() if self.get_active_symbols else []
        if interval == '1m' and symbol not in active_symbols:
            colored_log(self.logger, 'debug', f"[{symbol}] No active positions - skipping 1m data lookback fetch", success=True)
            return pd.DataFrame()

        if interval == "nifty_15m":
            interval = "15m"

        query = f"""
            SELECT * FROM ohlc_{interval}
            WHERE symbol = %s AND time >= %s AND time < %s
            ORDER BY time ASC
        """
        if interval == '1h':
            df = pd.read_sql(query, self.db_conn, params=('NIFTY', start_day, end_day + timedelta(days=1)))
        elif interval == 'nifty_15m':
            df = pd.read_sql(query, self.db_conn, params=('NIFTY', start_day, end_day + timedelta(days=1)))
        else:            
            df = pd.read_sql(query, self.db_conn, params=(symbol, start_day, end_day + timedelta(days=1)))

        if df.empty:
            if interval == '1h':
                colored_log(self.logger, 'warning', f"No data found for NIFTY {interval} between {start_day} and {end_day}", success=False)
            elif interval == 'nifty_15m':
                colored_log(self.logger, 'warning', f"No data found for NIFTY {interval} between {start_day} and {end_day}", success=False)
            else:
                colored_log(self.logger, 'warning', f"No data found for {symbol} {interval} between {start_day} and {end_day}", success=False)
        return df

    def relative_momentum_index(self, close, window=50):
        """RMI is a more responsive alternative to ADX."""
        delta = close.diff(1)
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window).mean()
        avg_loss = loss.rolling(window).mean()
        rmi = 100 * (avg_gain / (avg_gain + avg_loss))
        return rmi

    def exclude_first_30min(self, group):
        """Calculate expanding mean of range excluding first 30 minutes"""
        try:
            if group.empty or 'range' not in group.columns:
                return pd.Series(dtype=float)
                
            mask = ~(
                (group['time'].dt.time >= time(3, 45)) & 
                (group['time'].dt.time < time(4, 15)) # Time in UTC - hence 3.45 to 4.15
            )
            filtered_group = group[mask]
            if filtered_group.empty:
                return pd.Series(dtype=float, index=group.index)
                
            result = filtered_group['range'].expanding().mean()
            # Ensure we return a Series aligned with the original group
            return result.reindex(group.index).ffill()
        except Exception as e:
            # Return a Series of NaNs if there's an error
            return pd.Series(float('nan'), index=group.index)
    
    def zero_lag_ema(self, series, period):
        ema1 = series.ewm(span=period, adjust=False).mean()
        ema2 = ema1.ewm(span=period, adjust=False).mean()
        return ema1 + (ema1 - ema2)

    def hull_moving_average(self, close, window=50):
        """HMA reduces lag significantly vs EMA."""
        wma_half = talib.WMA(close, timeperiod=window//2)
        wma_full = talib.WMA(close, timeperiod=window)
        hma = talib.WMA(2 * wma_half - wma_full, timeperiod=int(np.sqrt(window)))
        return hma

    def zero_lag_macd(self, close, fast=12, slow=26, signal=9):
        """MACD using Zero-Lag EMAs (TEMA)."""
        ema_fast = talib.TEMA(close, timeperiod=fast)
        ema_slow = talib.TEMA(close, timeperiod=slow)
        macd = ema_fast - ema_slow
        signal_line = talib.TEMA(macd, timeperiod=signal)
        return macd, signal_line

    def classify_trend(self, row, interval):
        # Primary Conditions (1H)
        ema_bullish = row['close'] > row[f'nifty_{interval}_ema_50'] > row[f'nifty_{interval}_ema_200']
        ema_bearish = row['close'] < row[f'nifty_{interval}_ema_50'] < row[f'nifty_{interval}_ema_200']
        #hma_bullish = row['close'] > row[f'nifty_{interval}_hma_50'] > row[f'nifty_{interval}_hma_200']
        #hma_bearish = row['close'] < row[f'nifty_{interval}_hma_50'] < row[f'nifty_{interval}_hma_200']
        rmi_strong = row[f'nifty_{interval}_RMI'] > 60  # RMI > 60 = strong trend
        adx_strong = row[f'nifty_{interval}_adx'] > 20
        di_bullish = row[f'nifty_{interval}_+DI'] > row[f'nifty_{interval}_-DI']
        di_bearish = row[f'nifty_{interval}_-DI'] > row[f'nifty_{interval}_+DI']
        macd_bullish = row[f'nifty_{interval}_MACD'] > row[f'nifty_{interval}_MACD_Signal']
        macd_bearish = row[f'nifty_{interval}_MACD'] < row[f'nifty_{interval}_MACD_Signal']
        #volume_ok = row['Volume_Spike']
        
        # Trend Logic
        if ema_bullish and adx_strong and di_bullish and macd_bullish:
            return 1
        elif ema_bearish and adx_strong and di_bearish and macd_bearish:
            return -1
        else:
            return 0

    def calculate_all_indicators_once(self, df_all_dict, symbol):
        """
        Calculate all indicators once for the entire dataset
        Returns: Dictionary with pre-calculated dataframes
        """
        #self.logger.info(f"Calculating indicators once for entire dataset for {symbol}")
        
        # Extract dataframes
        df_15m = df_all_dict['15m'].copy()
        df_5m = df_all_dict['5m'].copy()
        df_1m = df_all_dict['1m'].copy()
        df_daily = df_all_dict['d'].copy()
        df_nifty_15m = df_all_dict['nifty_15m'].copy()
        
        
        # === ENSURE TIME COLUMNS ARE DATETIME ===
        for df in [df_15m, df_5m, df_1m, df_daily, df_nifty_15m]:
            if not df.empty:
                df['time'] = pd.to_datetime(df['time'])
        
        # === EARLY RETURN IF CRITICAL DATA IS MISSING ===
        if df_15m.empty or df_5m.empty or df_daily.empty or df_nifty_15m.empty:
            colored_log(self.logger, 'warning', f"Missing critical data for {symbol} - skipping indicator calculations", success=False)
            return {
                '15m': df_15m,
                '5m': df_5m,
                '1m': df_1m,
                'd': df_daily,
                'nifty_15m': df_nifty_15m
            }
       
        # === DAILY INDICATORS (ATR & Volume) ===
        atr_period = 14
        volume_period = 14
        df_daily['prev_close'] = df_daily['close'].shift(1)
        df_daily['tr1'] = df_daily['high'] - df_daily['low']
        df_daily['tr2'] = abs(df_daily['high'] - df_daily['prev_close'])
        df_daily['tr3'] = abs(df_daily['low'] - df_daily['prev_close'])
        df_daily['tr'] = df_daily[['tr1', 'tr2', 'tr3']].max(axis=1)
        df_daily['atr_10'] = df_daily['tr'].ewm(span=10, adjust=False).mean()
        df_daily['volume_10'] = df_daily['volume'].rolling(window=10).mean()
        df_daily['atr_14'] = df_daily['tr'].ewm(span=14, adjust=False).mean()
        df_daily['volume_14'] = df_daily['volume'].rolling(window=14).mean()
        df_daily['close_10'] = df_daily['close'].rolling(window=10).mean()
        df_daily['close_14'] = df_daily['close'].rolling(window=14).mean()
        df_daily['rsi_14'] = talib.RSI(df_daily['close'], timeperiod=14)
        df_daily['adx_14'] = talib.ADX(df_daily['high'], df_daily['low'], df_daily['close'], timeperiod=14)
        df_daily.drop(['prev_close', 'tr1', 'tr2', 'tr3', 'tr'], axis=1, inplace=True)
        
        # Add date columns for merging
        df_15m['date'] = pd.to_datetime(df_15m['time'].dt.date)
        df_5m['date'] = pd.to_datetime(df_5m['time'].dt.date)
        df_daily['date'] = pd.to_datetime(df_daily['time'].dt.date)
        df_nifty_15m['date'] = pd.to_datetime(df_nifty_15m['time'].dt.date)
        
        
        # Merge ATR from daily data
        df_15m = df_15m[['time', 'date', 'symbol', 'open', 'high', 'low', 'close', 'volume']].merge(df_daily[['date', 'atr_10', 'volume_10', 'close_10', 'atr_14', 'volume_14', 'close_14']], on='date', how='left')
        df_5m = df_5m[['time', 'date', 'symbol', 'open', 'high', 'low', 'close', 'volume']].merge(df_daily[['date', 'atr_10', 'volume_10', 'close_10', 'atr_14', 'volume_14', 'close_14']], on='date', how='left')
        
        # === NIFTY 15m INDICATORS (Nifty 50EMA) ===
        df_nifty_15m = df_nifty_15m.merge(df_daily[['date', 'atr_10', 'volume_10', 'close_10', 'atr_14', 'volume_14', 'close_14', 'rsi_14', 'adx_14']], on='date', how='left')    

        
        df_nifty_15m['nifty_15m_ema_50'] = df_nifty_15m['close'].ewm(span=50, adjust=False).mean()
        df_nifty_15m['nifty_15m_ema_200'] = df_nifty_15m['close'].ewm(span=200, adjust=False).mean()
        df_nifty_15m['nifty_15m_adx'] = talib.ADX(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
        df_nifty_15m['nifty_15m_+DI'] = talib.PLUS_DI(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
        df_nifty_15m['nifty_15m_-DI'] = talib.MINUS_DI(df_nifty_15m['high'], df_nifty_15m['low'], df_nifty_15m['close'], timeperiod=125)
        df_nifty_15m['nifty_15m_MACD'], df_nifty_15m['nifty_15m_MACD_Signal'], _ = talib.MACD(df_nifty_15m['close'], fastperiod=20, slowperiod=50, signalperiod=10)
        df_nifty_15m['nifty_15m_Volume_MA20'] = talib.MA(df_nifty_15m['volume'], timeperiod=20)
        df_nifty_15m['nifty_15m_Volume_Spike'] = df_nifty_15m['volume'] > 1.5 * df_nifty_15m['nifty_15m_Volume_MA20']
        df_nifty_15m['nifty_15m_RSI'] = talib.RSI(df_nifty_15m['close'], timeperiod=14)
        df_nifty_15m['nifty_15m_volume_sma_20'] = df_nifty_15m['volume'].rolling(window=20).mean()
        df_nifty_15m['nifty_15m_RVOL'] = df_nifty_15m['volume'] / df_nifty_15m['nifty_15m_volume_sma_20']
        
        df_nifty_15m['nifty_15m_RMI'] = self.relative_momentum_index(df_nifty_15m['close'])
        
        df_nifty_15m['nifty_trend_15m'] = df_nifty_15m.apply(self.classify_trend, args=('15m',), axis=1)

        # Merge trend into the 5min and 15min df
        # Keep only the first record each day
        df_nifty_15m = df_nifty_15m.groupby('date').last().reset_index()
        df_nifty_15m['nifty_trend_15m'] = df_nifty_15m['nifty_trend_15m'].shift(1)
        df_15m = df_15m.merge(df_nifty_15m[['date', 'nifty_trend_15m']], on='date', how='left')
        df_5m = df_5m.merge(df_nifty_15m[['date', 'nifty_trend_15m']], on='date', how='left')
        
        # === 5MIN INDICATORS (EMAs) ===
        df_5m['ema_50'] = df_5m['close'].ewm(span=50, adjust=False).mean()
        df_5m['ema_100'] = df_5m['close'].ewm(span=100, adjust=False).mean()
        df_5m['ema_200'] = df_5m['close'].ewm(span=200, adjust=False).mean()
        
        # === RANGE CALCULATIONS - 15m ===
        df_15m['range'] = df_15m['high'] - df_15m['low']
        df_15m['date_only'] = df_15m['time'].dt.date
        df_15m['avg_range_all'] = df_15m.groupby('date_only')['range'].expanding().mean().reset_index(level=0, drop=True)
        
        # Fix for "Cannot set a DataFrame with multiple columns" error
        try:
            avg_ex_first_30min_15m = df_15m.groupby('date_only').apply(self.exclude_first_30min)
            # Ensure we get a Series, not DataFrame
            if isinstance(avg_ex_first_30min_15m, pd.DataFrame):
                avg_ex_first_30min_15m = avg_ex_first_30min_15m.iloc[:, 0]  # Take first column
            avg_ex_first_30min_15m = avg_ex_first_30min_15m.reset_index(level=0, drop=True)
            df_15m['avg_range_ex_first_30min'] = avg_ex_first_30min_15m.reindex(df_15m.index).ffill()
        except Exception as e:
            colored_log(self.logger, 'warning', f"Error calculating avg_range_ex_first_30min for 15m: {e}, using avg_range_all instead", success=False)
            df_15m['avg_range_ex_first_30min'] = df_15m['avg_range_all']
        df_15m['is_range_bullish'] = (
            (df_15m['range'] > 0.7 * df_15m['avg_range_ex_first_30min']) & 
            (df_15m['close'] > df_15m['open']) & 
            (df_15m['close'] > (((df_15m['high'] - df_15m['open']) * 0.5) + df_15m['open']))
        )
        df_15m['is_range_bearish'] = (
            (df_15m['range'] > 0.7 * df_15m['avg_range_ex_first_30min']) & 
            (df_15m['close'] < df_15m['open']) & 
            (df_15m['close'] < (((df_15m['open'] - df_15m['low']) * 0.5) + df_15m['low']))
        )
        df_15m.drop('date_only', axis=1, inplace=True)
        
        # === RANGE CALCULATIONS - 5m ===
        df_5m['range'] = df_5m['high'] - df_5m['low']
        df_5m['date_only'] = df_5m['time'].dt.date
        df_5m['avg_range_all'] = df_5m.groupby('date_only')['range'].expanding().mean().reset_index(level=0, drop=True)
        
        # Fix for "Cannot set a DataFrame with multiple columns" error
        try:
            avg_ex_first_30min_5m = df_5m.groupby('date_only').apply(self.exclude_first_30min)
            # Ensure we get a Series, not DataFrame
            if isinstance(avg_ex_first_30min_5m, pd.DataFrame):
                avg_ex_first_30min_5m = avg_ex_first_30min_5m.iloc[:, 0]  # Take first column
            avg_ex_first_30min_5m = avg_ex_first_30min_5m.reset_index(level=0, drop=True)
            df_5m['avg_range_ex_first_30min'] = avg_ex_first_30min_5m.reindex(df_5m.index).ffill()
        except Exception as e:
            colored_log(self.logger, 'warning', f"Error calculating avg_range_ex_first_30min for 5m: {e}, using avg_range_all instead", success=False)
            df_5m['avg_range_ex_first_30min'] = df_5m['avg_range_all']
        df_5m['is_range_bullish'] = (
            (df_5m['range'] > 0.7 * df_5m['avg_range_ex_first_30min']) & 
            (df_5m['close'] > df_5m['open']) & 
            (df_5m['close'] > (((df_5m['high'] - df_5m['open']) * 0.5) + df_5m['open']))
        )
        df_5m['is_range_bearish'] = (
            (df_5m['range'] > 0.7 * df_5m['avg_range_ex_first_30min']) & 
            (df_5m['close'] < df_5m['open']) & 
            (df_5m['close'] < (((df_5m['open'] - df_5m['low']) * 0.5) + df_5m['low']))
        )
        df_5m.drop('date_only', axis=1, inplace=True)

        # === ZERO LAG MACD (15m only) ===
        fast_period, slow_period, signal_period = 12, 26, 9
        df_15m['fast_zlema'] = self.zero_lag_ema(df_15m['close'], fast_period)
        df_15m['slow_zlema'] = self.zero_lag_ema(df_15m['close'], slow_period)
        df_15m['zl_macd'] = df_15m['fast_zlema'] - df_15m['slow_zlema']
        df_15m['zl_signal'] = df_15m['zl_macd'].ewm(span=signal_period, adjust=False).mean()
        df_15m['zl_hist'] = df_15m['zl_macd'] - df_15m['zl_signal']
        
        # Generate MACD Signals
        df_15m['zl_macd_signal'] = 0
        df_15m.loc[(df_15m['zl_macd'] > df_15m['zl_signal']) & 
                (df_15m['zl_macd'].shift(1) <= df_15m['zl_signal'].shift(1)), 'zl_macd_signal'] = 1
        df_15m.loc[(df_15m['zl_macd'] < df_15m['zl_signal']) & 
                (df_15m['zl_macd'].shift(1) >= df_15m['zl_signal'].shift(1)), 'zl_macd_signal'] = -1
        df_15m.drop(['fast_zlema', 'slow_zlema', 'zl_macd', 'zl_signal', 'zl_hist'], axis=1, inplace=True)
        
        # === SINGLE PRINT CALCULATIONS - 15m ===
        df_15m['is_first_bullish_confirmed'] = False
        df_15m['is_first_bearish_confirmed'] = False
        df_15m['candle_count'] = df_15m.groupby(df_15m['date']).cumcount() + 1
        df_15m['cum_high_prev'] = df_15m.groupby('date')['high'].expanding().max().shift(1).reset_index(level=0, drop=True)
        df_15m['cum_low_prev'] = df_15m.groupby('date')['low'].expanding().min().shift(1).reset_index(level=0, drop=True)
        df_15m['cum_high'] = df_15m.groupby('date')['high'].expanding().max().reset_index(level=0, drop=True)
        df_15m['cum_low'] = df_15m.groupby('date')['low'].expanding().min().reset_index(level=0, drop=True)
        df_15m['sp_confirmed_bullish'] = (
            (df_15m['close'] > df_15m['cum_high_prev']) & 
            (df_15m['close'] > df_15m['open']) & 
            (df_15m['candle_count'] >= 2)
        )
        df_15m['sp_confirmed_bearish'] = (
            (df_15m['close'] < df_15m['cum_low_prev']) & 
            (df_15m['close'] < df_15m['open']) & 
            (df_15m['candle_count'] >= 2)
        )
        
        # Mark first confirmations
        bullish_conf_15m = df_15m[df_15m['sp_confirmed_bullish']]
        bearish_conf_15m = df_15m[df_15m['sp_confirmed_bearish']]
        first_bullish_idx_15m = bullish_conf_15m.groupby('date').head(1).index
        first_bearish_idx_15m = bearish_conf_15m.groupby('date').head(1).index
        df_15m.loc[first_bullish_idx_15m, 'is_first_bullish_confirmed'] = True
        df_15m.loc[first_bearish_idx_15m, 'is_first_bearish_confirmed'] = True
        
        # SP levels for 15m
        sp_levels_bullish_15m = df_15m[df_15m['is_first_bullish_confirmed']][['date', 'close', 'cum_high_prev']]
        sp_levels_bearish_15m = df_15m[df_15m['is_first_bearish_confirmed']][['date', 'close', 'cum_low_prev']]
        sp_levels_bullish_15m['sp_high_bullish'] = sp_levels_bullish_15m['close']
        sp_levels_bullish_15m['sp_low_bullish'] = sp_levels_bullish_15m['cum_high_prev']
        sp_levels_bearish_15m['sp_high_bearish'] = sp_levels_bearish_15m['cum_low_prev']
        sp_levels_bearish_15m['sp_low_bearish'] = sp_levels_bearish_15m['close']
        sp_levels_bullish_15m.drop(['close', 'cum_high_prev'], axis=1, inplace=True)
        sp_levels_bearish_15m.drop(['close', 'cum_low_prev'], axis=1, inplace=True)
        
        # Merge back for 15m
        df_15m = df_15m.merge(sp_levels_bullish_15m, on='date', how='left')
        df_15m = df_15m.merge(sp_levels_bearish_15m, on='date', how='left')
        
        # Forward fill SP levels for 15m
        df_15m['sp_high_bullish'] = df_15m.groupby('date')['sp_high_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_15m['sp_low_bullish'] = df_15m.groupby('date')['sp_low_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_15m['sp_high_bearish'] = df_15m.groupby('date')['sp_high_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_15m['sp_low_bearish'] = df_15m.groupby('date')['sp_low_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        
        # Set pre-confirmation values to NaN for 15m
        df_15m.loc[~df_15m['sp_confirmed_bullish'].cummax(), ['sp_high_bullish', 'sp_low_bullish']] = None
        df_15m.loc[~df_15m['sp_confirmed_bearish'].cummax(), ['sp_high_bearish', 'sp_low_bearish']] = None
        
        # Calculate SP range percentages for 15m
        df_15m['sp_bullish_range_pct'] = (df_15m['sp_high_bullish'] - df_15m['sp_low_bullish']) / df_15m['sp_low_bullish'] * 100
        df_15m['sp_bearish_range_pct'] = (df_15m['sp_high_bearish'] - df_15m['sp_low_bearish']) / df_15m['sp_low_bearish'] * 100
        df_15m['cum_sp_bullish'] = df_15m.groupby('date')['sp_confirmed_bullish'].cumsum()
        df_15m['cum_sp_bearish'] = df_15m.groupby('date')['sp_confirmed_bearish'].cumsum()
        
        # === SINGLE PRINT CALCULATIONS - 5m ===
        df_5m['is_first_bullish_confirmed'] = False
        df_5m['is_first_bearish_confirmed'] = False
        df_5m['candle_count'] = df_5m.groupby(df_5m['date']).cumcount() + 1
        df_5m['cum_high_prev'] = df_5m.groupby('date')['high'].expanding().max().shift(1).reset_index(level=0, drop=True)
        df_5m['cum_low_prev'] = df_5m.groupby('date')['low'].expanding().min().shift(1).reset_index(level=0, drop=True)
        df_5m['cum_high'] = df_5m.groupby('date')['high'].expanding().max().reset_index(level=0, drop=True)
        df_5m['cum_low'] = df_5m.groupby('date')['low'].expanding().min().reset_index(level=0, drop=True)
        df_5m['sp_confirmed_bullish'] = (
            (df_5m['close'] > df_5m['cum_high_prev']) & 
            (df_5m['close'] > df_5m['open']) & 
            (df_5m['candle_count'] >= 2)
        )
        df_5m['sp_confirmed_bearish'] = (
            (df_5m['close'] < df_5m['cum_low_prev']) & 
            (df_5m['close'] < df_5m['open']) & 
            (df_5m['candle_count'] >= 2)
        )
        
        # Mark first confirmations for 5m
        bullish_conf_5m = df_5m[df_5m['sp_confirmed_bullish']]
        bearish_conf_5m = df_5m[df_5m['sp_confirmed_bearish']]
        first_bullish_idx_5m = bullish_conf_5m.groupby('date').head(1).index
        first_bearish_idx_5m = bearish_conf_5m.groupby('date').head(1).index
        df_5m.loc[first_bullish_idx_5m, 'is_first_bullish_confirmed'] = True
        df_5m.loc[first_bearish_idx_5m, 'is_first_bearish_confirmed'] = True
        
        # SP levels for 5m
        sp_levels_bullish_5m = df_5m[df_5m['is_first_bullish_confirmed']][['date', 'close', 'cum_high_prev']]
        sp_levels_bearish_5m = df_5m[df_5m['is_first_bearish_confirmed']][['date', 'close', 'cum_low_prev']]
        sp_levels_bullish_5m['sp_high_bullish'] = sp_levels_bullish_5m['close']
        sp_levels_bullish_5m['sp_low_bullish'] = sp_levels_bullish_5m['cum_high_prev']
        sp_levels_bearish_5m['sp_high_bearish'] = sp_levels_bearish_5m['cum_low_prev']
        sp_levels_bearish_5m['sp_low_bearish'] = sp_levels_bearish_5m['close']
        sp_levels_bullish_5m.drop(['close', 'cum_high_prev'], axis=1, inplace=True)
        sp_levels_bearish_5m.drop(['close', 'cum_low_prev'], axis=1, inplace=True)
        
        # Merge back for 5m
        df_5m = df_5m.merge(sp_levels_bullish_5m, on='date', how='left')
        df_5m = df_5m.merge(sp_levels_bearish_5m, on='date', how='left')
        
        # Forward fill SP levels for 5m
        df_5m['sp_high_bullish'] = df_5m.groupby('date')['sp_high_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_5m['sp_low_bullish'] = df_5m.groupby('date')['sp_low_bullish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_5m['sp_high_bearish'] = df_5m.groupby('date')['sp_high_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        df_5m['sp_low_bearish'] = df_5m.groupby('date')['sp_low_bearish'].transform(lambda x: x.ffill() if x.notna().any() else x)
        
        # Set pre-confirmation values to NaN for 5m
        df_5m.loc[~df_5m['sp_confirmed_bullish'].cummax(), ['sp_high_bullish', 'sp_low_bullish']] = None
        df_5m.loc[~df_5m['sp_confirmed_bearish'].cummax(), ['sp_high_bearish', 'sp_low_bearish']] = None
        
        # Calculate SP range percentages for 5m
        df_5m['sp_bullish_range_pct'] = (df_5m['sp_high_bullish'] - df_5m['sp_low_bullish']) / df_5m['sp_low_bullish'] * 100
        df_5m['sp_bearish_range_pct'] = (df_5m['sp_high_bearish'] - df_5m['sp_low_bearish']) / df_5m['sp_low_bearish'] * 100
        df_5m['cum_sp_bullish'] = df_5m.groupby('date')['sp_confirmed_bullish'].cumsum()
        df_5m['cum_sp_bearish'] = df_5m.groupby('date')['sp_confirmed_bearish'].cumsum()
        
        # === VOLUME & RANGE CALCULATIONS - 15m ===
        df_15m['cum_intraday_volume'] = df_15m.groupby('date')['volume'].cumsum()
        df_15m['curtop'] = df_15m.groupby('date')['high'].cummax()
        df_15m['curbot'] = df_15m.groupby('date')['low'].cummin()
        df_15m['predicted_today_high'] = df_15m['curbot'] + df_15m['atr_10']
        df_15m['predicted_today_low'] = df_15m['curtop'] - df_15m['atr_10']
        df_15m['today_range'] = df_15m['curtop'] - df_15m['curbot']
        df_15m['today_range_pct_10'] = df_15m['today_range'] / df_15m['atr_10']
        df_15m['today_range_pct_14'] = df_15m['today_range'] / df_15m['atr_14']
        df_15m['volume_range_pct_10'] = (df_15m['cum_intraday_volume'] / df_15m['volume_10']) / df_15m['today_range_pct_10']
        df_15m['volume_range_pct_14'] = (df_15m['cum_intraday_volume'] / df_15m['volume_14']) / df_15m['today_range_pct_14']
        
        # === VOLUME & RANGE CALCULATIONS - 5m ===
        df_5m['cum_intraday_volume'] = df_5m.groupby('date')['volume'].cumsum()
        df_5m['curtop'] = df_5m.groupby('date')['high'].cummax()
        df_5m['curbot'] = df_5m.groupby('date')['low'].cummin()
        df_5m['predicted_today_high'] = df_5m['curbot'] + df_5m['atr_10']
        df_5m['predicted_today_low'] = df_5m['curtop'] - df_5m['atr_10']
        df_5m['today_range'] = df_5m['curtop'] - df_5m['curbot']
        df_5m['today_range_pct_10'] = df_5m['today_range'] / df_5m['atr_10']
        df_5m['today_range_pct_14'] = df_5m['today_range'] / df_5m['atr_14']
        df_5m['volume_range_pct_10'] = (df_5m['cum_intraday_volume'] / df_5m['volume_10']) / df_5m['today_range_pct_10']
        df_5m['volume_range_pct_14'] = (df_5m['cum_intraday_volume'] / df_5m['volume_14']) / df_5m['today_range_pct_14']
        
        # === STRATEGY DEFINITIONS ===
        # Strategy 8 & 12 (15m)
        df_15m['s_8'] = (
            (df_15m['time'].dt.time >= time(4, 0)) & 
            (df_15m['time'].dt.time < time(8, 15)) & 
            (df_15m['cum_sp_bullish'] >= 1) & 
            (df_15m['sp_bullish_range_pct'] > 0.8) & 
            (df_15m['sp_bullish_range_pct'] < 1.3) & 
            (df_15m['zl_macd_signal'] == -1) & 
            (df_15m['volume_range_pct_10'] > 1) &
            (df_15m['atr_10'] / df_15m['close_10'] < 0.04) &
            (df_15m['nifty_trend_15m'] >= 0)
        )
        df_15m['strategy_8'] = False
        first_true_idx_8 = df_15m[df_15m['s_8']].groupby('date').head(1).index
        df_15m.loc[first_true_idx_8, 'strategy_8'] = True
        
        df_15m['s_12'] = (
            (df_15m['time'].dt.time >= time(4, 0)) & 
            (df_15m['time'].dt.time < time(8, 15)) & 
            (df_15m['cum_sp_bearish'] >= 1) & 
            (df_15m['sp_bearish_range_pct'] > 1) & 
            (df_15m['zl_macd_signal'] == 1) &
            (df_15m['volume_range_pct_10'] > 0) &
            (df_15m['volume_range_pct_10'] < 0.4) &
            (df_15m['atr_10'] / df_15m['close_10'] < 0.04) &
            (df_15m['nifty_trend_15m'] <= 0)
        )
        df_15m['strategy_12'] = False
        first_true_idx_12 = df_15m[df_15m['s_12']].groupby('date').head(1).index
        df_15m.loc[first_true_idx_12, 'strategy_12'] = True
        
        
        # Strategy 10 & 11 (5m)
        df_5m['s_10'] = (
            (df_5m['time'].dt.time >= time(3, 50)) & 
            (df_5m['time'].dt.time < time(8, 15)) & 
            (df_5m['cum_sp_bearish'] >= 1) & 
            (df_5m['sp_bearish_range_pct'] > 0.6) & 
            (df_5m['close'] < df_5m['ema_50']) & 
            (df_5m['close'] < df_5m['ema_100']) & 
            (df_5m['close'] < df_5m['ema_200']) & 
            (df_5m['is_range_bearish']) & 
            (df_5m['volume_range_pct_10'] > 0.3) & 
            (df_5m['volume_range_pct_10'] < 0.7) & 
            (df_5m['atr_10'] / df_5m['close_10'] > 0.04) &            
            (df_5m['nifty_trend_15m'] != 1)
        )
        df_5m['strategy_10'] = False
        first_true_idx_10 = df_5m[df_5m['s_10']].groupby('date').head(1).index
        df_5m.loc[first_true_idx_10, 'strategy_10'] = True
        
        df_5m['s_11'] = (
            (df_5m['time'].dt.time >= time(3, 50)) & 
            (df_5m['time'].dt.time < time(8, 15)) & 
            (df_5m['cum_sp_bullish'] >= 1) & 
            (df_5m['sp_bullish_range_pct'] > 0.8) & 
            (df_5m['close'] > df_5m['ema_50']) & 
            (df_5m['close'] > df_5m['ema_100']) & 
            (df_5m['close'] > df_5m['ema_200']) & 
            (df_5m['is_range_bullish']) & 
            (df_5m['volume_range_pct_10'] > 0) & 
            (df_5m['volume_range_pct_10'] < 0.3) &
            (df_5m['atr_10'] / df_5m['close_10'] > 0.04) &
            (df_5m['nifty_trend_15m'] != -1)
        )
        df_5m['strategy_11'] = False
        first_true_idx_11 = df_5m[df_5m['s_11']].groupby('date').head(1).index
        df_5m.loc[first_true_idx_11, 'strategy_11'] = True
        
        df_5m['s_9'] = (
            (df_5m['time'].dt.time >= time(3, 50)) & 
            (df_5m['time'].dt.time < time(8, 15)) & 
            (df_5m['cum_sp_bullish'] >= 1) & 
            (df_5m['sp_bullish_range_pct'] > 0.8) & 
            (df_5m['close'] > df_5m['ema_50']) & 
            (df_5m['close'] > df_5m['ema_100']) & 
            (df_5m['close'] > df_5m['ema_200']) & 
            (df_5m['is_range_bullish']) & 
            (df_5m['volume_range_pct_10'] > 0.3) & 
            (df_5m['volume_range_pct_10'] < 0.6) &
            (df_5m['atr_10'] / df_5m['close_10'] < 0.04) &
            (df_5m['nifty_trend_15m'] != 1)
        )
        df_5m['strategy_9'] = False
        first_true_idx_9 = df_5m[df_5m['s_9']].groupby('date').head(1).index
        df_5m.loc[first_true_idx_9, 'strategy_9'] = True
        
        # Clean up date columns
        df_15m.drop('date', axis=1, inplace=True)
        df_5m.drop('date', axis=1, inplace=True)
        
             
        return {
            '15m': df_15m,
            '5m': df_5m,
            '1m': df_1m,
            'd': df_daily,
            'nifty_15m': df_nifty_15m
        }

    def chunk_dates(self, start_date, end_date, chunk_size_days):
        current = start_date
        while current <= end_date:
            next_chunk = min(current + timedelta(days=chunk_size_days - 1), end_date)
            yield current, next_chunk
            current = next_chunk + timedelta(days=1)
            
    
    def _update_indicators_in_db(self, df, symbol, timeframe):
        """Update indicators in database"""
        try:
            # Get only the latest row for each timeframe
            df = df.sort_values('time').groupby(df.index).last()
            
            cursor = self.db_conn.cursor()            
            
            indicator_columns = ['atr_10', 'volume_10', 'nifty_trend_15m', 'curbot', 'curtop', 'cum_intraday_volume', 'strategy_8', 'strategy_9', 'strategy_10', 'strategy_11', 'strategy_12']
            
            for _, row in df.iterrows():
                # Build update query for available indicators
                update_parts = []
                values = []
                
                for col in indicator_columns:
                    if col in row and pd.notna(row[col]):
                        update_parts.append(f"{col} = %s")
                        # Convert boolean strategy values to integers
                        if col.startswith('nifty_trend_15m') or col.startswith('volume_') or col.startswith('cum_intraday_volume'):
                            values.append(int(row[col]))
                        elif col.startswith('atr_') or col.startswith('curbot') or col.startswith('curtop'):
                            values.append(float(row[col]))
                        elif col.startswith('strategy_'):
                            values.append(bool(row[col]))
                        else:
                            values.append(row[col])
                
                if update_parts:
                    # Add symbol and time at the end of values list
                    values.extend([symbol, row['time']])
                    
                    query = f"""
                        UPDATE ohlc_{timeframe}
                        SET {', '.join(update_parts)}
                        WHERE symbol = %s AND time = %s
                    """                    
                    
                    cursor.execute(query, values)
            
            self.db_conn.commit()
            cursor.close()
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error updating indicators in DB for {symbol} {timeframe}: {e}", success=False)
            self.db_conn.rollback()

    def process_symbol_interval(self, symbol, interval, client, start_date, end_date, mode):
        """Process a single symbol-interval pair"""
        try:
            if interval == "5m" or interval == "1m":
                # Chunk the dates into smaller ranges to avoid timeout
                s_d = datetime.strptime(start_date, "%Y-%m-%d").date()
                e_d = datetime.strptime(end_date, "%Y-%m-%d").date()
                #self.logger.info(f"Fetching data for {symbol} with interval {interval} from {s_d} to {e_d}")
                
                for chunk_start, chunk_end in self.chunk_dates(start_date=s_d, end_date=e_d, chunk_size_days=10):
                    self.fetch_historical_data(
                        symbol, 
                        interval, 
                        client, 
                        chunk_start.strftime("%Y-%m-%d"),
                        chunk_end.strftime("%Y-%m-%d"),
                        mode
                    )
            else:    
                self.fetch_historical_data(symbol, interval, client, start_date, end_date, mode)
        except Exception as e:
            self.logger.error(f"Error processing {symbol} {interval}: {str(e)}")

    def fetch_historical_data(self, symbol, interval, client, start_date, end_date, mode):
        try:
            # Check for interrupt flag
            if self.interrupt_flag:
                colored_log(self.logger, 'info', f"[{symbol}] Task interrupted before starting", success=True)
                return
                
            # Add retry logic with exponential backoff
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Check for interrupt before each retry
                    if self.interrupt_flag:
                        colored_log(self.logger, 'info', f"[{symbol}] Task interrupted during retry {attempt + 1}", success=True)
                        return
                    df = client.history(
                            symbol=symbol,
                            exchange='NSE_INDEX' if symbol.startswith('NIFTY') else 'NSE',
                            interval=interval,
                            start_date=start_date,
                            end_date=end_date
                        )
                    
                    # Check if df is dictionary (error response)
                    if isinstance(df, dict):                    
                        if 'timeout' in str(df.get('message', '')).lower():
                            if attempt < max_retries - 1:
                                wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff
                                colored_log(self.logger, 'warning', f"[{symbol}] Timeout on attempt {attempt + 1}, retrying in {wait_time:.1f}s...", success=False)
                                time_module.sleep(wait_time)
                                continue
                        colored_log(self.logger, 'warning', f"[{symbol}] API Response error! No data on {start_date}", success=False)
                        colored_log(self.logger, 'info', f"API Response: {df}", success=True)
                        # Remove the symbol from the symbols list
                        self.symbols.remove(symbol)
                        colored_log(self.logger, 'info', f"[{symbol}] Removed from symbols list", success=True)
                        return  # Exit function
                    
                    # Success - process the dataframe
                    if hasattr(df, 'empty') and not df.empty:
                        self.insert_historical_data(df, symbol, interval, mode)
                    else:
                        colored_log(self.logger, 'warning', f"[{symbol}] Empty Dataframe! No data on {start_date}", success=False)
                    return  # Exit function on success
                    
                except Exception as retry_e:
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        colored_log(self.logger, 'warning', f"[{symbol}] Error on attempt {attempt + 1}: {retry_e}, retrying in {wait_time:.1f}s...", success=False)
                        time_module.sleep(wait_time)
                    else:
                        colored_log(self.logger, 'error', f"[{symbol}] Failed after {max_retries} attempts: {retry_e}", success=False)
            
            # Add delay between requests to reduce server load
            time_module.sleep(random.uniform(0.1, 0.3))

        except Exception as e:
            colored_log(self.logger, 'error', f"[{symbol}] Error during fetch: {e}", success=False)

    def fetch_intraday_data(self, symbol, interval, client, start_date, end_date, start_time, end_time, mode):
        try:
            # Check for interrupt flag
            if self.interrupt_flag:
                colored_log(self.logger, 'info', f"[{symbol}] Task interrupted before starting", success=True)
                return
                
            # Add retry logic with exponential backoff
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Check for interrupt before each retry
                    if self.interrupt_flag:
                        colored_log(self.logger, 'info', f"[{symbol}] Task interrupted during retry {attempt + 1}", success=True)
                        return                    
                    
                    success, response, status_code = get_history_intraday(
                        symbol=symbol,
                        exchange='NSE_INDEX' if symbol.startswith('NIFTY') else 'NSE',
                        interval=interval,
                        start_date=start_date,
                        end_date=end_date,
                        start_time=start_time,
                        end_time=end_time,
                        api_key=self.api_key
                    )
                    
                    if not success:
                        raise Exception(f"Intraday API Error: {response.get('message', 'Unknown error')}")
                        
                    df = pd.DataFrame(response['data']) if response.get('data') else pd.DataFrame()
                    colored_log(self.logger, 'debug', f"Intraday Data before processing:\n{df.head()}\nColumns: {df.columns.tolist()}", success=True)
                    
                    # Check if df is dictionary (error response)
                    if isinstance(df, dict):                    
                        if 'timeout' in str(df.get('message', '')).lower():
                            if attempt < max_retries - 1:
                                wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff
                                colored_log(self.logger, 'warning', f"[{symbol}] ⏳ Timeout on attempt {attempt + 1}, retrying in {wait_time:.1f}s...", success=False)
                                time_module.sleep(wait_time)
                                continue
                        colored_log(self.logger, 'warning', f"[{symbol}] API Response error! No data on {start_date} {start_time} to {end_date} {end_time}", success=False)
                        colored_log(self.logger, 'info', f"API Response: {df}", success=True)
                        return  # Exit function
                    
                    # Success - process the dataframe
                    if hasattr(df, 'empty') and not df.empty:
                        self.insert_historical_data(df, symbol, interval, mode)
                    else:
                        colored_log(self.logger, 'warning', f"[{symbol}] ⚠️ Empty Dataframe! No data on {start_date}", success=False)
                    return  # Exit function on success
                    
                except Exception as retry_e:
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        colored_log(self.logger, 'warning', f"[{symbol}] ⏳ Error on attempt {attempt + 1}: {retry_e}, retrying in {wait_time:.1f}s...", success=False)
                        time_module.sleep(wait_time)
                    else:
                        colored_log(self.logger, 'error', f"[{symbol}] ❌ Failed after {max_retries} attempts: {retry_e}", success=False)
            
            # Add delay between requests to reduce server load
            time_module.sleep(random.uniform(0.1, 0.3))

        except Exception as e:
            colored_log(self.logger, 'error', f"[{symbol}] ❌ Error during fetch: {e}", success=False)


    def insert_historical_data(self, df, symbol, interval, mode):
        """
        Insert historical data into the appropriate database table
        
        Args:
            df (pd.DataFrame): DataFrame containing historical data
            symbol (str): Stock symbol (e.g., 'RELIANCE')
            interval (str): Time interval ('1m', '5m', '15m', '1d')
        """
        try:
            # Ensure we have a pandas DataFrame
            if not isinstance(df, pd.DataFrame):
                df = pd.DataFrame(df)

            if df.empty:
                colored_log(self.logger, 'warning', f"No data to insert for {symbol} {interval}", success=False)
                return False
            
            # Handle different data formats
            try:
                if isinstance(df.index, pd.DatetimeIndex):
                    # Case 1: Data with datetime index (most common for historical data)
                    df = df.reset_index()
                    if df.columns[0] == 'index':
                        df = df.rename(columns={'index': 'time'})
                    elif df.columns[0] == 'timestamp':
                        df = df.rename(columns={'timestamp': 'time'})
                    
                elif 'timestamp' in list(df.columns):
                    # Case 2: Data with timestamp column
                    df['time'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
                    df = df.drop('timestamp', axis=1)
                    
                elif df.index.name == 'time':
                    # Case 3: Data with time index
                    df = df.reset_index()
                    
                else:
                    colored_log(self.logger, 'error', f"No time data found for {symbol} {interval}", success=False)
                    return False
                
                # Verify time column exists
                if 'time' not in df.columns:
                    colored_log(self.logger, 'error', f"Time column missing after conversion for {symbol} {interval}", success=False)
                    return False
                
            except Exception as e:
                colored_log(self.logger, 'error', f"Error processing time data: {e}", success=False)
                import traceback
                colored_log(self.logger, 'error', traceback.format_exc(), success=False)
                return False
            
            # Add symbol column if not present
            if 'symbol' not in df.columns:
                df['symbol'] = symbol
                
            # Ensure we have the required columns
            required_columns = ['time', 'symbol', 'open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_columns):
                colored_log(self.logger, 'error', f"Missing required column in data for {symbol} {interval}: 'time'", success=False)
                colored_log(self.logger, 'error', f"Available columns: {df.columns.tolist()}", success=False)
                colored_log(self.logger, 'error', f"Index type: {type(df.index)}, Index name: {df.index.name}", success=False)
                return False
            
            # Add debug logging after processing
            colored_log(self.logger, 'debug', f"Data after processing:\n{df.head()}\nColumns: {df.columns.tolist()}", success=True)
            
            # Handle timezone conversion differently for intraday vs daily data
            df['time'] = pd.to_datetime(df['time'])
            if interval == 'D':
                # Set to market open time (09:15:00 IST) for each date
                df['time'] = df['time'].dt.tz_localize(None)  # Remove any timezone
                df['time'] = df['time'] + pd.Timedelta(hours=9, minutes=15)
                df['time'] = df['time'].dt.tz_localize('Asia/Kolkata')
            else:
                if df['time'].dt.tz is None:
                    df['time'] = df['time'].dt.tz_localize('Asia/Kolkata')
                else:
                    df['time'] = df['time'].dt.tz_convert('Asia/Kolkata')
            
            # Convert to UTC for database storage
            df['time'] = df['time'].dt.tz_convert('UTC')

            # Add symbol column
            df['symbol'] = symbol
            
            # Select and order the columns we need (excluding 'oi' which we don't store)
            required_columns = ['time', 'symbol', 'open', 'high', 'low', 'close', 'volume']
            df = df[required_columns]
            
            # Convert numeric columns to appropriate types
            numeric_cols = ['open', 'high', 'low', 'close']
            df[numeric_cols] = df[numeric_cols].astype(float)
            df['volume'] = df['volume'].astype(int)
            
            # Determine the target table based on interval
            table_name = f'ohlc_{interval.lower()}'
            
            # Convert DataFrame to list of tuples
            records = [tuple(x) for x in df.to_numpy()]
            
            # Debug: print first record to verify format
            colored_log(self.logger, 'debug', f"First record sample: {records[0] if records else 'No records'}", success=True)
           
            conn = self.db_conn            
            
            with conn.cursor() as cursor:
                # Use execute_batch for efficient bulk insertion
                execute_batch(cursor, f"""
                    INSERT INTO {table_name} 
                    (time, symbol, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (time, symbol) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume
                """, records)
                
                conn.commit()
                colored_log(self.logger, 'debug', f"Successfully inserted {len(df)} records for {symbol} ({interval}) into {table_name}", success=True)
                return True
                
        except KeyError as e:
            colored_log(self.logger, 'error', f"Missing required column in data for {symbol} {interval}: {e}", success=False)
            colored_log(self.logger, 'error', f"Available columns: {df.columns.tolist()}", success=False)
            return False
        except Exception as e:
            colored_log(self.logger, 'error', f"Error inserting historical data for {symbol} {interval}: {e}", success=False)
            colored_log(self.logger, 'error', traceback.format_exc(), success=False)
            conn.rollback()
            return False

    
    def start_real_time_data(self):
        """Start real-time data processing thread"""
        if not self.is_running:
            self.is_running = True
            self.data_thread = threading.Thread(target=self._real_time_data_loop, daemon=True)
            self.data_thread.start()
            colored_log(self.logger, 'info', "Real-time data processing started", success=True)
    
    def stop_real_time_data(self):
        """Stop real-time data processing"""
        # Unsubscribe and disconnect from real-time data
        self.api_client.unsubscribe_quote(self.instruments_list)
        self.api_client.disconnect()

        self.is_running = False
        if self.data_thread:
            self.data_thread.join(timeout=5)
        colored_log(self.logger, 'info', "Real-time data processing stopped", success=True)
    
    def _real_time_data_loop(self):
        """Main loop for processing real-time tick data using TimescaleDB's message processing"""
        heartbeat_check_interval = 120  # Changed to 120 seconds for all timeframes
        last_heartbeat_check = time_module.time()
        colored_log(self.logger, 'info', "Starting real-time data monitoring (30s interval)", success=True)

        while self.is_running:
            try:
                # Check if market is open
                current_time = datetime.now(IST).time()
                if current_time < self.market_start or current_time >= self.market_end:
                    colored_log(self.logger, 'info', "Market closed. Waiting for market hours...", success=True)
                    time_module.sleep(60)  # Increased sleep when market is closed
                    continue

                # Try to use Kafka-based message processing first
                if hasattr(self, 'consumer') and self.consumer:
                    self._process_messages_non_blocking()

                # Check heartbeat every 60 seconds during market hours for all timeframes
                current_time = time_module.time()
                if current_time - last_heartbeat_check >= heartbeat_check_interval:
                    colored_log(self.logger, 'debug', "Running data continuity check for all timeframes...", success=True)
                    self.heartbeat_monitor.check_data_continuity(self) # Pass self for recovery
                    last_heartbeat_check = current_time                

                # Small delay to prevent excessive CPU usage
                time_module.sleep(1)
                
            except Exception as e:
                colored_log(self.logger, 'error', f"Error in real-time data loop: {e}", success=False)
                time_module.sleep(5)  # Wait before retrying
                    
    def on_data_received(self, data):
        pass

    def _process_messages_non_blocking(self):
        """Process message by directly fetching from the historical API"""
        try:
            with self.lock:
                current_time = datetime.now(IST)
                current_date = current_time.date()
                current_minute = current_time.minute
                
                # Initialize last fetch times if not exists
                if not hasattr(self, '_last_fetch_times'):
                    self._last_fetch_times = {
                        '1m': None,
                        '5m': None,
                        '15m': None
                    }
                
                # Helper function to check if we should fetch for an interval
                def should_fetch(interval_key, interval_minutes):
                    if current_minute % interval_minutes != 0:
                        return False
                    
                    last_fetch = self._last_fetch_times[interval_key]
                    if last_fetch is None or (current_time - last_fetch).total_seconds() >= 55:  # Allow 5s buffer
                        self._last_fetch_times[interval_key] = current_time
                        return True
                    return False

                # Fetch 1-min data - only for symbols with active positions (optimization)
                if should_fetch('1m', 1):
                    start_time = (current_time - timedelta(minutes=2)).time().strftime('%H:%M:%S') # 2 minutes buffer
                    end_time = (current_time + timedelta(minutes=1)).time().strftime('%H:%M:%S')
                    #nd_time = current_time.time().strftime('%H:%M:%S')
                    
                    # Get symbols with active positions for 1m data (used for exit signals)
                    active_symbols = self.get_active_symbols() if self.get_active_symbols else []

                    colored_log(self.logger, 'info', f"Fetching 1m data for active symbols: {active_symbols}", success=True)
                    
                    if active_symbols:
                        date_str = current_date.strftime('%Y-%m-%d')
                        for symbol in active_symbols:
                            self.fetch_intraday_data(symbol, '1m', self.api_client, date_str, date_str, start_time, end_time, 'recovery')
                            self.on_new_candle(symbol, '1m', current_time.time())
                        colored_log(self.logger, 'info', f"Loaded 1-min data for {len(active_symbols)} active position symbols: {active_symbols}", success=True)
                    else:
                        colored_log(self.logger, 'debug', "No active positions - skipping 1m data fetch", success=True)

                # Fetch 5-min data
                if should_fetch('5m', 5):
                    start_time = (current_time - timedelta(minutes=6)).time().strftime('%H:%M:%S') # 6 minutes buffer
                    #end_time = (current_time + timedelta(minutes=5)).time().strftime('%H:%M:%S')
                    end_time = current_time.time().strftime('%H:%M:%S')

                    colored_log(self.logger, 'info', f"Fetching 5m data for {len(self.symbols)} symbols in parallel. Current time: {current_time.time()}", success=True)
                    
                    date_str = current_date.strftime('%Y-%m-%d')
                    self._fetch_data_parallel(self.symbols, '5m', date_str, start_time, end_time, current_time.time())
                    colored_log(self.logger, 'info', "Loaded 5-min data for all symbols", success=True)                    

                # Fetch 15-min data
                if should_fetch('15m', 15):
                    start_time = (current_time - timedelta(minutes=16)).time().strftime('%H:%M:%S') # 16 minutes buffer
                    #end_time = (current_time + timedelta(minutes=15)).time().strftime('%H:%M:%S')
                    end_time = current_time.time().strftime('%H:%M:%S')

                    colored_log(self.logger, 'info', f"Fetching 15m data for {len(self.symbols)} symbols in parallel. Current time: {current_time.time()}", success=True)
                    
                    date_str = current_date.strftime('%Y-%m-%d')
                    self._fetch_data_parallel(self.symbols, '15m', date_str, start_time, end_time, current_time.time())
                    colored_log(self.logger, 'info', "Loaded 15-min data for all symbols", success=True)

        except Exception as e:
            colored_log(self.logger, 'error', f"Error in non-blocking message processing: {e}", success=False)

    def _fetch_data_parallel(self, symbols, interval, date_str, start_time, end_time, current_candle_time):
        """Fetch data for multiple symbols in parallel to improve performance"""
        import time as time_module
        
        def fetch_single_symbol(symbol):
            """Fetch data for a single symbol and process new candle"""
            try:
                self.fetch_intraday_data(symbol, interval, self.api_client, date_str, date_str, start_time, end_time, 'recovery')
                self.on_new_candle(symbol, interval, current_candle_time)
                return f"✅ {symbol}"
            except Exception as e:
                colored_log(self.logger, 'error', f"Error fetching {interval} data for {symbol}: {e}", success=False)
                return f"❌ {symbol}: {e}"
        
        start_parallel_time = time_module.time()
        
        # Use ThreadPoolExecutor for parallel processing
        with ThreadPoolExecutor(max_workers=8) as executor:  # Limit concurrent requests
            futures = {executor.submit(fetch_single_symbol, symbol): symbol for symbol in symbols}
            results = []
            
            for future in futures:
                try:
                    result = future.result(timeout=30)  # 30 second timeout per symbol
                    results.append(result)
                except Exception as e:
                    symbol = futures[future]
                    colored_log(self.logger, 'error', f"Timeout/Error for {symbol} {interval}: {e}", success=False)
                    results.append(f"❌ {symbol}: timeout/error")
        
        parallel_time = time_module.time() - start_parallel_time
        successful_fetches = len([r for r in results if r.startswith('✅')])
        
        colored_log(self.logger, 'info', 
                  f"Parallel {interval} fetch completed: {successful_fetches}/{len(symbols)} symbols in {parallel_time:.2f}s", 
                  success=True)
       
    def _process_messages_non_blocking_(self):
        """Process messages non-blocking way using TimescaleDB's consumer"""
        try:
            
            # Poll for messages with a short timeout
            raw_msg = self.consumer.poll(100.0)  # 100ms timeout
            
            if raw_msg is None:
                return  # No messages available

            #self.logger.info(f"Raw message: {raw_msg}")
            
            # Process received messages
            for topic_partition, messages in raw_msg.items():    
                for message in messages:
                    try:
                        # Extract key and value
                        key = message.key.decode('utf-8')  # 'NSE_RELIANCE_LTP', 'NSE_INDEX_NIFTY_LTP'
                        value = json.loads(message.value.decode('utf-8'))

                        #self.logger.info(f"Processing {key}: {value['symbol']}@{value['close']}")

                        # Process the message using TimescaleDB's method
                        self.process_single_message(key, value)

                    except Exception as e:
                        colored_log(self.logger, 'error', f"Error processing message: {e}", success=False)
                        
        except Exception as e:
            colored_log(self.logger, 'error', f"Error in non-blocking message processing: {e}", success=False)

    def process_single_message(self, key, value):
        """Process extracted tick data"""
        try:
            # Extract components from key
            if key.startswith('NSE_INDEX_NIFTY'):
                components = key.split('_')
                exchange = components[0]  # 'NSE'
                dummy = components[1]    # 'INDEX'
                symbol = components[2]    # 'NIFTY'
                data_type = components[3] # 'LTP' or 'QUOTE'
            else:    
                components = key.split('_')
                exchange = components[0]  # 'NSE'
                symbol = components[1]    # 'RELIANCE'
                data_type = components[2] # 'LTP' or 'QUOTE'

             # Convert timestamp (handling milliseconds since epoch)
            timestamp = value['timestamp']
            if not isinstance(timestamp, (int, float)):
                raise ValueError(f"Invalid timestamp type: {type(timestamp)}")

            # Convert to proper datetime object
            # Ensure milliseconds (not seconds or microseconds)
            if timestamp < 1e12:  # Likely in seconds
                timestamp *= 1000
            elif timestamp > 1e13:  # Likely in microseconds
                timestamp /= 1000
                
            dt = datetime.fromtimestamp(timestamp / 1000, tz=pytz.UTC)  
            
            # Validate date range
            if dt.year < 2020 or dt.year > 2030:
                raise ValueError(f"Implausible date {dt} from timestamp {timestamp}")
                       
            # Prepare database record
            record = {
                'time': dt,  # Convert ms to seconds
                'symbol': symbol,
                'open': float(value['ltp']),
                'high': float(value['ltp']),
                'low': float(value['ltp']),
                'close': float(value['ltp']),
                'volume': int(value['volume'])
            }

            #self.logger.info(f"Record---------> {record}")
            
            # Store in TimescaleDB
            self.store_tick(record)  

            # Add to aggregation buffers
            self.buffer_tick(record)

            # Check for aggregation opportunities
            self.check_aggregation(record['time'])
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Tick processing failed: {e}", success=False)
            colored_log(self.logger, 'debug', traceback.format_exc(), success=True)


    def store_tick(self, record):
        """Store raw tick in database"""
        try:
            with self.db_conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO ticks (time, symbol, open, high, low, close, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (time, symbol) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                    """, (record['time'], record['symbol'], record['open'], record['high'], record['low'], record['close'], record['volume']))
                self.db_conn.commit()
        except Exception as e:
            colored_log(self.logger, 'error', f"Error storing tick: {e}", success=False)
            self.db_conn.rollback()

    def buffer_tick(self, record):
        """Add tick to aggregation buffers"""
        with self.lock:
            for timeframe in ['1m', '5m', '15m']:
                minutes = int(timeframe[:-1])
                symbol = record['symbol']
                aligned_time = self.floor_to_interval(record['time'], minutes)                

                if symbol not in self.tick_buffer[timeframe]:
                    self.tick_buffer[timeframe][symbol] = {}

                # Initialize this specific minute bucket
                if aligned_time not in self.tick_buffer[timeframe][symbol]:
                    self.tick_buffer[timeframe][symbol][aligned_time] = {
                        'opens': [],
                        'highs': [],
                        'lows': [],
                        'closes': [],
                        'volumes': [],
                        'first_tick': None  # Track the first tick separately
                    }
                bucket = self.tick_buffer[timeframe][symbol][aligned_time]

                # For the first tick in this interval, store it separately
                if bucket['first_tick'] is None:
                    bucket['first_tick'] = record

                bucket['opens'].append(record['open'])
                bucket['highs'].append(record['high'])
                bucket['lows'].append(record['low'])
                bucket['closes'].append(record['close'])
                bucket['volumes'].append(record['volume'])

    def check_aggregation(self, current_time):
        """Check if aggregation should occur for any timeframe"""
        timeframes = ['1m', '5m', '15m']
        
        for timeframe in timeframes:
            agg_interval = timedelta(minutes=int(timeframe[:-1]))
            last_agg = self.last_agg_time[timeframe]
            
            #self.logger.info(f"{timeframe}: current_time={current_time}, last_agg={last_agg}, interval={agg_interval}")

            if current_time - last_agg >= agg_interval:
                if self.aggregate_data(timeframe, current_time):
                    self.last_agg_time[timeframe] = self.floor_to_interval(current_time, int(timeframe[:-1]))
            
    def aggregate_data(self, timeframe, agg_time):
        with self.lock:
            symbol_buckets = self.tick_buffer[timeframe]
            if not symbol_buckets:
                return False

            aggregated = []
            table_name = f"ohlc_{timeframe}"

            for symbol, buckets in symbol_buckets.items():
                for bucket_start, data in list(buckets.items()):
                    if bucket_start >= self.last_agg_time[timeframe] + timedelta(minutes=int(timeframe[:-1])):
                        # Don't process future buckets
                        continue

                    if not data['opens']:
                        continue
                    
                    try:
                        # Get OHLC values
                        if data['first_tick'] is not None:
                            open_ = data['first_tick']['open']
                        else:
                            open_ = data['opens'][0]

                        #open_ = data['opens'][0]
                        high = max(data['highs'])
                        low = min(data['lows'])
                        close = data['closes'][-1]      

                        # Calculate volume correctly for cumulative data
                        current_last_volume = data['volumes'][-1]
                        previous_last_volume = self.last_period_volume[timeframe].get(symbol, current_last_volume)
                        volume = max(0, current_last_volume - previous_last_volume)

                        # Store the current last volume for next period
                        self.last_period_volume[timeframe][symbol] = current_last_volume

                        candle = {
                            'time': bucket_start,
                            'symbol': symbol,
                            'open': open_,
                            'high': high,
                            'low': low,
                            'close': close,
                            'volume': volume
                        }
   
                        aggregated.append(candle)

                        # Notify trading engine of new candle
                        # self.on_new_candle(symbol, timeframe, candle)

                        # Remove this bucket to avoid re-aggregation
                        del self.tick_buffer[timeframe][symbol][bucket_start]
                    
                    except Exception as e:
                        self.logger.error(f"Error aggregating {symbol} for {timeframe}: {e}")
                        continue

            # Aggregate only if volume is greater than 0
            if aggregated and sum(c['volume'] for c in aggregated) > 0:
                try:
                    with self.db_conn.cursor() as cursor:
                        execute_batch(cursor, f"""
                            INSERT INTO {table_name} 
                            (time, symbol, open, high, low, close, volume)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (time, symbol) DO UPDATE SET
                                open = EXCLUDED.open,
                                high = EXCLUDED.high,
                                low = EXCLUDED.low,
                                close = EXCLUDED.close,
                                volume = EXCLUDED.volume
                            """, [(c['time'], c['symbol'], c['open'], c['high'], c['low'], c['close'], c['volume']) for c in aggregated])
                        self.db_conn.commit()
                    
                    # Notify trading engine of new candle
                    for c in aggregated:
                        self.on_new_candle(c['symbol'], timeframe, c)

                    colored_log(self.logger, 'info', f"Aggregated {len(aggregated)} symbols to {table_name}", success=True)
                    return True
                except Exception as e:
                    colored_log(self.logger, 'error', f"Error aggregating {timeframe} data: {e}", success=False)
                    self.db_conn.rollback()
                    return False
            elif aggregated and sum(c['volume'] for c in aggregated) == 0:
                # Show log only if symbol != 'NIFTY'
                for c in aggregated:
                    if c['symbol'] != 'NIFTY':
                        colored_log(self.logger, 'info', f"No volume for {timeframe} data for {c['symbol']}", success=True)
                return True
            return False


    def get_latest_data(self, symbol, timeframe, rows=1):
        """Get latest N rows of data for symbol/timeframe from PostgreSQL"""
        try:
            query = f"""
                SELECT time, open, high, low, close, volume,
                       strategy_8, strategy_9, strategy_10, strategy_11, strategy_12,
                       atr_10, volume_10, atr_14, volume_14, nifty_trend_15m, curbot, curtop, cum_intraday_volume
                FROM ohlc_{timeframe}
                WHERE symbol = %s 
                ORDER BY time DESC
                LIMIT %s
            """
            
            df = pd.read_sql(query, self.db_conn, params=(symbol, rows))
            
            if not df.empty:
                df['time'] = pd.to_datetime(df['time'])
                df = df.sort_values('time').reset_index(drop=True)
                return df
            else:
                return pd.DataFrame()
                
        except Exception as e:
            colored_log(self.logger, 'error', f"Error fetching latest data for {symbol} {timeframe}: {e}", success=False)
            return pd.DataFrame()
    
    def get_current_signals(self, symbol):
        """Get current trading signals for symbol"""
        signals = {
            'strategy_8': False,
            'strategy_12': False,
            'strategy_10': False,
            'strategy_11': False,
            'strategy_9': False
        }
        
        try:
            # Get latest 15m data
            df_15m = self.get_latest_data(symbol, '15m', 1)
            if not df_15m.empty:
                latest_15m = df_15m.iloc[-1]
                # Latest time has to be within 16 minutes(15 minutes plus 1 minute buffer) of current time to avoid stale data
                if latest_15m['time'] > datetime.now(IST) - timedelta(minutes=16):
                    signals['strategy_8'] = latest_15m.get('strategy_8', False)
                    signals['strategy_12'] = latest_15m.get('strategy_12', False)
            
            # Get latest 5m data
            df_5m = self.get_latest_data(symbol, '5m', 1)
            if not df_5m.empty:
                latest_5m = df_5m.iloc[-1]
                # Latest time has to be within 6 minutes(5 minutes plus 1 minute buffer) of current time to avoid stale data
                if latest_5m['time'] > datetime.now(IST) - timedelta(minutes=6):
                    signals['strategy_10'] = latest_5m.get('strategy_10', False)
                    signals['strategy_11'] = latest_5m.get('strategy_11', False)
                    signals['strategy_9'] = latest_5m.get('strategy_9', False)
            
        except Exception as e:
            colored_log(self.logger, 'error', f"Error getting signals for {symbol}: {e}", success=False)
        
        return signals

class OrderManager:
    """Manages order execution"""
    def __init__(self, api_client):
        self.api_client = api_client
        self.logger = logging.getLogger("OrderManager")
        
    def place_order(self, symbol, quantity, side, order_type="MARKET"):
        """Place order through API"""
        try:
            # Determine exchange (assuming NSE for all)
            exchange = "NSE"
            
            # Place order via API
            order_response = self.api_client.placeorder(
                strategy="LiveTrading",
                symbol=symbol,
                action=side,  # BUY or SELL
                exchange=exchange,
                price_type=order_type,
                product="MIS",  # Intraday
                quantity=str(quantity),
                trigger_price="0",
                disclosed_quantity="0"
            )

            # Validate order response
            if isinstance(order_response, tuple) and len(order_response) == 3:
                success, response_data, status_code = order_response
                
                if success and status_code == 200:
                    colored_log(self.logger, 'info', f"ORDER PLACED: {side} {quantity} {symbol} | Response: {response_data}", success=True)
                    return response_data.get('orderid') if isinstance(response_data, dict) else None
                else:
                    error_msg = response_data.get('message', 'Unknown error') if isinstance(response_data, dict) else str(response_data)
                    colored_log(self.logger, 'error', f"ORDER FAILED: {symbol} {side} {quantity} | Error: {error_msg} | Status: {status_code}", success=False)
                    return None
            else:
                colored_log(self.logger, 'error', f"ORDER FAILED: {symbol} {side} {quantity} | Invalid response format: {order_response}", success=False)
                return None
                
        except Exception as e:
            colored_log(self.logger, 'error', f"ORDER FAILED: {symbol} {side} {quantity} | Error: {e}", success=False)
            return None

    def place_sl_order(self, symbol, quantity, side, sl_price=None, order_type="SL"):
            """Place order through API"""
            try:
                # Determine exchange (assuming NSE for all)
                exchange = "NSE"

                if side == "SELL" and sl_price is not None:
                    limit_price = sl_price - 1;
                elif side == "BUY" and sl_price is not None:
                    limit_price = sl_price + 1;
                
                # Place order via API
                order_response = self.api_client.placeorder(
                    strategy="LiveTrading",
                    symbol=symbol,
                    action=side,  # BUY or SELL
                    exchange=exchange,
                    price_type=order_type,
                    product="MIS",  # Intraday
                    quantity=str(quantity),
                    trigger_price=str(sl_price),
                    price=str(limit_price),
                    disclosed_quantity="0"
                )

                # Validate order response
                if isinstance(order_response, tuple) and len(order_response) == 3:
                    success, response_data, status_code = order_response
                    
                    if success and status_code == 200:
                        colored_log(self.logger, 'info', f"SL ORDER PLACED: {side} {quantity} {symbol} | Response: {response_data}", success=True)
                        return response_data.get('orderid') if isinstance(response_data, dict) else None
                    else:
                        error_msg = response_data.get('message', 'Unknown error') if isinstance(response_data, dict) else str(response_data)
                        colored_log(self.logger, 'error', f"SL ORDER FAILED: {symbol} {side} {quantity} | Error: {error_msg} | Status: {status_code}", success=False)
                        return None
                else:
                    colored_log(self.logger, 'error', f"SL ORDER FAILED: {symbol} {side} {quantity} | Invalid response format: {order_response}", success=False)
                    return None
                    
            except Exception as e:
                colored_log(self.logger, 'error', f"SL ORDER FAILED: {symbol} {side} {quantity} | Error: {e}", success=False)
                return None

    def cancel_order(self, order_id):
        """Cancel order through API"""
        try:
            # Cancel order via API
            order_response = self.api_client.cancelorder(orderid=order_id)

            # Validate order response
            if isinstance(order_response, tuple) and len(order_response) == 3:
                success, response_data, status_code = order_response
                
                if success and status_code == 200:
                    colored_log(self.logger, 'info', f"ORDER CANCELLED: {order_id}", success=True)
                    return True
            
            colored_log(self.logger, 'error', f"ORDER CANCEL FAILED: {order_id} | Invalid response format: {order_response}", success=False)
            return False
            
        except Exception as e:
            colored_log(self.logger, 'error', f"ORDER CANCEL FAILED: {order_id} | Error: {e}", success=False)
            return False
           
    def modify_order(self, order_id, symbol, quantity, side, sl_price, order_type="MARKET"):
        """Modify order through API"""
        try:
            # Determine exchange (assuming NSE for all)
            exchange = "NSE"

            if side == "SELL" and sl_price is not None:
                limit_price = sl_price - 1;
            elif side == "BUY" and sl_price is not None:
                limit_price = sl_price + 1;

            # Modify order via API
            order_response = self.api_client.modifyorder(
                strategy="LiveTrading",
                symbol=symbol,
                action=side,
                exchange=exchange,
                orderid=order_id,
                product="MIS",
                pricetype=order_type,
                price=str(limit_price),
                quantity=str(quantity),
                disclosed_quantity="0",
                trigger_price=str(sl_price)
            )
            
            # Validate order response
            if isinstance(order_response, tuple) and len(order_response) == 3:
                success, response_data, status_code = order_response
                
                if success and status_code == 200:
                    colored_log(self.logger, 'info', f"ORDER MODIFIED: {order_id}", success=True)
                    return True
            
            colored_log(self.logger, 'error', f"ORDER MODIFY FAILED: {order_id} | Invalid response format: {order_response}", success=False)
            return False
            
        except Exception as e:
            colored_log(self.logger, 'error', f"ORDER MODIFY FAILED: {order_id} | Error: {e}", success=False)
            return False
            
class LiveTradingEngine:
    """Main Live Trading Engine"""
    def __init__(self, api_key, host, ws_url, db_config, symbols):
        self.api_client = api(api_key=api_key, host=host, ws_url=ws_url)
        self.api_key = api_key
        self.symbols = symbols
        self.running = False
        self.shutdown_event = threading.Event()      
        
        # Threading
        self.scan_thread = None
        self.monitor_thread = None
        
        # Logging
        self.logger = logging.getLogger("LiveTradingEngine")
        
        # Market hours
        self.market_start = TRADING_CONFIG['market_start']
        self.market_end = TRADING_CONFIG['market_end']
        self.trading_start = TRADING_CONFIG['trading_start']  # Start trading 5 min after market open
        self.trading_end = TRADING_CONFIG['trading_end']   # Stop trading 20 min before market close

        # Position Sizing
        self.capital = TRADING_CONFIG['capital']  # 1 Lakh
        self.leverage = TRADING_CONFIG['leverage']
        self.capital_alloc_pct = TRADING_CONFIG['capital_alloc_pct']  # 30% per trade
        
        # Risk Management
        self.sl_pct = TRADING_CONFIG['sl_pct']
        self.tp_pct = TRADING_CONFIG['tp_pct']
        self.trail_activation_pct = TRADING_CONFIG['trail_activation_pct']
        self.trail_stop_gap_pct = TRADING_CONFIG['trail_stop_gap_pct']
        self.trail_increment_pct = TRADING_CONFIG['trail_increment_pct']  

        # Trade constraints
        self.max_open_positions = TRADING_CONFIG['max_open_positions']
        self.max_daily_trades = TRADING_CONFIG['max_daily_trades']
        self.max_strategy_trades_per_day = TRADING_CONFIG['max_strategy_trades_per_day']

        # Components
        self.position_manager = PositionManager(self.api_client, self.sl_pct, self.tp_pct, self.trail_activation_pct, self.trail_stop_gap_pct, self.trail_increment_pct, self.trading_start, self.trading_end, self.max_open_positions, self.max_daily_trades, self.max_strategy_trades_per_day)
        self.data_manager = LiveDataManager(db_config, self.api_client, self.api_key, self.symbols, self.market_start, self.market_end, self.get_active_symbols)
        self.order_manager = OrderManager(self.api_client)    
        
        colored_log(self.logger, 'info', f"LiveTradingEngine initialized for {len(symbols)} symbols", success=True)
    
    def get_active_symbols(self):
        """Return list of symbols with active positions"""
        return list(self.position_manager.open_positions.keys()) if self.position_manager and self.position_manager.open_positions else []
        
    def is_market_hours(self):
        """Check if market is open"""
        current_time = datetime.now(IST).time()
        current_day = datetime.now(IST).weekday()
        
        # Monday = 0, Sunday = 6
        if current_day >= 5:  # Weekend
            return False
        
        return self.market_start <= current_time <= self.market_end
    
    def is_trading_hours(self):
        """Check if we should be trading"""
        current_time = datetime.now(IST).time()
        return self.is_market_hours() and self.trading_start <= current_time <= self.trading_end
    
    def scan_symbols(self):
        """Main symbol scanning loop"""
        colored_log(self.logger, 'info', "Starting symbol scanner", success=True)
        
        while not self.shutdown_event.is_set():
            try:
                if not self.is_trading_hours():
                    colored_log(self.logger, 'info', "Outside trading hours, waiting...", success=True)
                    time_module.sleep(30)
                    continue
                
                current_time = datetime.now(IST)
                colored_log(self.logger, 'debug', f"Scanning symbols at {current_time.strftime('%H:%M:%S')}", success=True)
                
                # Scan all symbols for entry signals
                for symbol in self.symbols:
                    if self.shutdown_event.is_set():
                        break
                    
                    try:
                        self._process_symbol_entry(symbol, current_time)
                    except Exception as e:
                        colored_log(self.logger, 'error', f"Error processing {symbol}: {e}", success=False)
                
                # Short delay between scans
                time_module.sleep(20)  # 20 second scan frequency
                
            except Exception as e:
                colored_log(self.logger, 'error', f"Error in symbol scanner: {e}", success=False)
                time_module.sleep(5)
    
    def _process_symbol_entry(self, symbol, current_time):
        """Process entry signals for symbol"""
        current_date = current_time.date()
        
        # Skip if already have position
        if symbol in self.position_manager.open_positions:
            return
        
        # Get current signals
        signals = self.data_manager.get_current_signals(symbol)
        
        # Check for long entries (Strategy 12 and 11)
        long_signal = signals['strategy_12'] or signals['strategy_11']
        if long_signal:
            colored_log(self.logger, 'info', f"LONG SIGNAL:: SYMBOL: {symbol} | STRATEGY: {strategy} | TIME: {current_time.strftime('%H:%M:%S')}", success=True)
            strategy = '12' if signals['strategy_12'] else '11'
            can_trade, reason = self.position_manager.can_open_position(symbol, strategy, current_date)
            
            if can_trade:
                self._execute_entry(symbol, 'LONG', strategy, current_time)
            else:
                colored_log(self.logger, 'debug', f"Cannot enter LONG {symbol} Strategy {strategy}: {reason}", success=True)
        
        # Check for short entries (Strategy 8, 10, 9)
        short_signal = signals['strategy_8'] or signals['strategy_10'] or signals['strategy_9']
        if short_signal:
            colored_log(self.logger, 'info', f"SHORT SIGNAL:: SYMBOL: {symbol} | STRATEGY: {strategy} | TIME: {current_time.strftime('%H:%M:%S')}", success=True)
            strategy = '8' if signals['strategy_8'] else ('10' if signals['strategy_10'] else '9')
            can_trade, reason = self.position_manager.can_open_position(symbol, strategy, current_date)
            
            if can_trade:
                self._execute_entry(symbol, 'SHORT', strategy, current_time)
            else:
                colored_log(self.logger, 'debug', f"Cannot enter SHORT {symbol} Strategy {strategy}: {reason}", success=True)
    
    def _execute_entry(self, symbol, direction, strategy, timestamp):
        """Execute entry order"""
        try:
            colored_log(self.logger, 'info', f"EXECUTING ENTRY:: SYMBOL: {symbol} | DIRECTION: {direction} | STRATEGY: {strategy} | TIME: {timestamp.strftime('%H:%M:%S')}", success=True)
            # Get current price from latest data
            latest_data = self.data_manager.get_latest_data(symbol, '1m', 1)
            if latest_data.empty:
                colored_log(self.logger, 'warning', f"No current price data for {symbol}", success=False)
                return
            
            current_price = latest_data.iloc[-1]['close']
            sl_price = current_price * (1 - self.sl_pct / 100) if direction == "LONG" else current_price * (1 + self.sl_pct / 100)
            tp_price = current_price * (1 + self.tp_pct / 100) if direction == "LONG" else current_price * (1 - self.tp_pct / 100)
            trail_activation_price = current_price * (1 + self.trail_activation_pct / 100) if direction == "LONG" else current_price * (1 - self.trail_activation_pct / 100)
            
            # Calculate position size (risk-based)
            capital_per_trade = self.capital * self.capital_alloc_pct * self.leverage / 100 # 30% of 1L capital
            quantity = int(capital_per_trade / current_price)
            
            if quantity <= 0:
                colored_log(self.logger, 'warning', f"Invalid quantity for {symbol}: {quantity}", success=False)
                return
            
            # Place order
            side = "BUY" if direction == "LONG" else "SELL"
            sl_side = "SELL" if direction == "LONG" else "BUY"
            order_response = self.order_manager.place_order(symbol, quantity, side)
            sl_order_response = self.order_manager.place_sl_order(symbol, quantity, sl_side, sl_price)
            
            if order_response:
                colored_log(self.logger, 'info', f"ENTRY PLACED:: SYMBOL: {symbol} | DIRECTION: {direction} | STRATEGY: {strategy} | ORDER ID: {order_response} | SL ORDER ID: {sl_order_response} | TIME: {timestamp.strftime('%H:%M:%S')}", success=True)
                # Record position (assuming order filled at current price)
                self.position_manager.open_position(
                    symbol=symbol,
                    strategy=strategy,
                    direction=direction,
                    entry_price=current_price,
                    quantity=quantity,
                    timestamp=timestamp,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    trail_activation_price=trail_activation_price,       
                    order_id=order_response,
                    sl_order_id=sl_order_response
                )
                
        except Exception as e:
            colored_log(self.logger, 'error', f"Error executing entry for {symbol}: {e}", success=False)
    
    def monitor_positions(self):
        """Monitor open positions for exits"""
        colored_log(self.logger, 'info', "Starting position monitor", success=True)
        
        while not self.shutdown_event.is_set():
            try:
                if not self.is_market_hours():
                    time_module.sleep(30)
                    continue
                
                current_time = datetime.now(IST)
                
                # Check all open positions
                positions_to_close = []
                positions_to_modify = []
                
                for symbol, position in self.position_manager.open_positions.items():
                    try:
                        # Get current price
                        latest_data = self.data_manager.get_latest_data(symbol, '1m', 1)
                        if latest_data.empty:
                            continue
                        
                        current_price = latest_data.iloc[-1]['close']                        
                        
                        # Update trailing stop
                        should_modify_trailing_stop, new_trail_stop_price = self.position_manager.update_trailing_stop(symbol, current_price)
                        
                        if should_modify_trailing_stop:
                            positions_to_modify.append((symbol, new_trail_stop_price))
                        
                        # Check exit conditions
                        should_exit, exit_reason = self.position_manager.check_exit_conditions(symbol, current_price)
                        
                        if should_exit:
                            positions_to_close.append((symbol, current_price, exit_reason))
                        
                    except Exception as e:
                        colored_log(self.logger, 'error', f"Error monitoring {symbol}: {e}", success=False)
                
                # Execute trailing stop modifications
                for symbol, new_trail_stop_price in positions_to_modify:
                    self._modify_trailing_stop(symbol, new_trail_stop_price)

                # Execute exits
                for symbol, exit_price, exit_reason in positions_to_close:
                    self._execute_exit(symbol, exit_price, exit_reason, current_time)
                
                time_module.sleep(2)  # Check every 2 seconds
                
            except Exception as e:
                colored_log(self.logger, 'error', f"Error in position monitor: {e}", success=False)
                time_module.sleep(5)

    def _modify_trailing_stop(self, symbol, new_trail_stop_price):
        """Modify trailing stop"""
        try:
            position = self.position_manager.open_positions.get(symbol)
            if not position:
                return

            side = "SELL" if position['direction'] == "LONG" else "BUY"
            
            order_response = self.order_manager.modify_order(position['order_id'], position['symbol'], position['quantity'], side, new_trail_stop_price, "SL")
            
            if order_response:
                colored_log(self.logger, 'info', f"TRAILING STOP MODIFIED: {symbol} {new_trail_stop_price}", success=True)
            else:
                colored_log(self.logger, 'error', f"TRAILING STOP MODIFICATION FAILED: {symbol} {new_trail_stop_price}", success=False)
        
        except Exception as e:
            colored_log(self.logger, 'error', f"Error modifying trailing stop for {symbol}: {e}", success=False)
    
    def _execute_exit(self, symbol, exit_price, exit_reason, timestamp):
        """Execute exit order"""
        try:
            position = self.position_manager.open_positions.get(symbol)
            if not position:
                return
            
            side = "SELL" if position['direction'] == "LONG" else "BUY"
            
            # If exit reason is TP, then Close the position and cancel SL order
            if exit_reason == "TP":
                self.order_manager.cancel_order(position['sl_order_id'])
                order_response = self.order_manager.place_order(symbol, position['quantity'], side)
                if order_response:
                    trade_record = self.position_manager.close_position(symbol, exit_price, exit_reason, order_response, timestamp)
                    if trade_record:
                        self._log_trade(trade_record)
                    return

            # If exit reason is EOD, then Close the position and cancel SL order
            if exit_reason == "EOD":
                self.order_manager.cancel_order(position['sl_order_id'])
                order_response = self.order_manager.place_order(symbol, position['quantity'], side)
                if order_response:
                    trade_record = self.position_manager.close_position(symbol, exit_price, exit_reason, order_response, timestamp)
                    if trade_record:
                        self._log_trade(trade_record)
                    return           
                
        except Exception as e:
            colored_log(self.logger, 'error', f"Error executing exit for {symbol}: {e}", success=False)
    
    def _log_trade(self, trade_record):
        """Log completed trade"""
        colored_log(self.logger, 'info', f"TRADE COMPLETED: {trade_record['symbol']} {trade_record['direction']} | "
                        f"Entry: {trade_record['entry_price']} | Exit: {trade_record['exit_price']} | "
                        f"P&L: {trade_record['gross_pnl']} | Strategy: {trade_record['strategy']}", success=True)
    
    def start(self):
        """Start the live trading engine"""
        if self.running:
            colored_log(self.logger, 'warning', "Engine already running", success=False)
            return
        
        self.running = True
        self.shutdown_event.clear()
        
        colored_log(self.logger, 'info', "Starting Live Trading Engine", success=True)
        
        # Start real-time data processing
        self.data_manager.start_real_time_data()
        
        # Start threads
        self.scan_thread = threading.Thread(target=self.scan_symbols, daemon=True)
        self.monitor_thread = threading.Thread(target=self.monitor_positions, daemon=True)
        
        self.scan_thread.start()
        self.monitor_thread.start()
        
        colored_log(self.logger, 'info', "Live Trading Engine started successfully", success=True)
    
    def stop(self):
        """Stop the live trading engine"""
        if not self.running:
            return
        
        colored_log(self.logger, 'info', "Stopping Live Trading Engine", success=True)
        
        self.running = False
        self.shutdown_event.set()
        
        # Stop real-time data processing
        self.data_manager.stop_real_time_data()
        
        # Wait for threads
        if self.scan_thread:
            self.scan_thread.join(timeout=10)
        if self.monitor_thread:
            self.monitor_thread.join(timeout=10)
        
        # Close any remaining positions at market close
        if self.position_manager.open_positions:
            colored_log(self.logger, 'info', "Closing remaining positions", success=True)
            current_time = datetime.now(IST)
            for symbol in list(self.position_manager.open_positions.keys()):
                latest_data = self.data_manager.get_latest_data(symbol, '1m', 1)
                if not latest_data.empty:
                    current_price = latest_data.iloc[-1]['close']
                    self._execute_exit(symbol, current_price, "EOD", current_time)
        
        colored_log(self.logger, 'info', "Live Trading Engine stopped", success=True)
    
    def get_status(self):
        """Get current status"""
        return {
            'running': self.running,
            'market_hours': self.is_market_hours(),
            'trading_hours': self.is_trading_hours(),
            'open_positions': len(self.position_manager.open_positions),
            'daily_trades': self.position_manager.daily_trades.get(datetime.now(IST).date(), 0),
            'symbols_count': len(self.symbols)
        }

def main():
    """Main entry point for live trading"""
    # Configuration
    API_KEY = "8009e08498f085ff1a3e7da718c5f4b585eaf9c2b7ce0c72740ab2b5d283d36c"
    HOST = "http://127.0.0.1:5000"
    
    DB_CONFIG = {
        'user': os.getenv('TIMESCALE_DB_USER'),
        'password': os.getenv('TIMESCALE_DB_PASSWORD'),
        'host': os.getenv('TIMESCALE_DB_HOST'),
        'port': os.getenv('TIMESCALE_DB_PORT'),
        'dbname': os.getenv('TIMESCALE_DB_NAME_LIVE')
    }
    
    # Load symbols
    symbols = pd.read_csv('strategies/symbol_list_live.csv')['Symbol'].tolist()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f'live_trading_{datetime.now().strftime("%Y%m%d")}.log'),
            logging.StreamHandler()
        ]
    )
    
    logger = logging.getLogger("Main")
    
    # Signal handlers for graceful shutdown
    engine = None
    
    def signal_handler(signum, frame):
        colored_log(logger, 'info', "Shutdown signal received", success=True)
        if engine:
            engine.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Initialize engine
        engine = LiveTradingEngine(API_KEY, HOST, DB_CONFIG, symbols)
        
        # Start engine
        engine.start()
        
        # Keep main thread alive and show status
        while engine.running:
            status = engine.get_status()
            colored_log(logger, 'info', f"STATUS: Positions: {status['open_positions']} | "
                       f"Daily Trades: {status['daily_trades']} | "
                       f"Market: {'OPEN' if status['market_hours'] else 'CLOSED'} | "
                       f"Trading: {'ACTIVE' if status['trading_hours'] else 'INACTIVE'}", success=True)
            
            time_module.sleep(60)  # Status update every minute
        
    except KeyboardInterrupt:
        colored_log(logger, 'info', "Keyboard interrupt", success=True)
    except Exception as e:
        colored_log(logger, 'error', f"Fatal error: {e}", success=False)
    finally:
        if engine:
            engine.stop()

if __name__ == "__main__":
    main()
