# backend/app/services/analytics.py
import pandas as pd
import numpy as np
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class AnalyticsService:
    """
    Dedicated calculation engine for trading performance.
    Uses Pandas for vectorized operations (fast & efficient).
    """

    def __init__(self, trades: List[Dict[str, Any]]):
        # 1. Initialize DataFrame
        self.df = pd.DataFrame(trades)
        self.has_data = not self.df.empty
        
        if self.has_data:
            self._preprocess_data()

    def _preprocess_data(self):
        """Clean and type-cast data for analysis."""
        # Ensure numeric types (handle nulls as 0 for calculations)
        numeric_cols = ['pnl', 'entry_price', 'exit_price', 'quantity', 'fees']
        for col in numeric_cols:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors='coerce').fillna(0)
        
        # Ensure Datetime
        if 'entry_time' in self.df.columns:
            self.df['entry_time'] = pd.to_datetime(self.df['entry_time'])
        
        # Standardize Strings
        if 'status' in self.df.columns:
            self.df['status'] = self.df['status'].str.upper()

    def get_dashboard_stats(self) -> Dict[str, Any]:
        """
        Returns the complete payload for the main dashboard.
        """
        if not self.has_data:
            return self._empty_stats()

        # --- Core Calculations ---
        total_pnl = float(self.df['pnl'].sum())
        
        # Win/Loss Stats (Only consider CLOSED trades for accuracy)
        closed = self.df[self.df['status'] == 'CLOSED']
        
        if closed.empty:
            return self._empty_stats()

        wins = closed[closed['pnl'] > 0]
        losses = closed[closed['pnl'] <= 0]

        win_count = len(wins)
        loss_count = len(losses)
        total_count = len(closed)

        win_rate = (win_count / total_count * 100) if total_count > 0 else 0
        
        gross_profit = wins['pnl'].sum()
        gross_loss = abs(losses['pnl'].sum())
        
        # Profit Factor (Gross Profit / Gross Loss)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (100.0 if gross_profit > 0 else 0)
        
        avg_win = wins['pnl'].mean() if not wins.empty else 0
        avg_loss = losses['pnl'].mean() if not losses.empty else 0

        # Expectancy = (Win% * AvgWin) - (Loss% * AvgLoss)
        # Note: AvgLoss in formula is usually absolute
        expectancy = ((win_rate / 100) * avg_win) - ((1 - (win_rate / 100)) * abs(avg_loss))

        # --- Advanced: Max Drawdown ---
        # 1. Sort by date
        equity_curve = self.df.sort_values('entry_time').copy()
        # 2. Cumulative Sum
        equity_curve['equity'] = equity_curve['pnl'].cumsum()
        # 3. Running Max
        equity_curve['peak'] = equity_curve['equity'].cummax()
        # 4. Drawdown
        equity_curve['drawdown'] = equity_curve['equity'] - equity_curve['peak']
        max_drawdown = float(equity_curve['drawdown'].min()) # This will be negative

        # --- Charts Data ---
        
        # 1. Cumulative PnL (Equity Curve)
        # Downsample if > 1000 points to keep frontend fast
        if len(equity_curve) > 1000:
             equity_curve = equity_curve.iloc[::len(equity_curve)//500]
             
        cumulative_data = [
            {"date": row['entry_time'].strftime('%b %d'), "value": float(row['equity'])}
            for _, row in equity_curve.iterrows()
        ]

        # 2. Daily PnL Bar Chart
        daily = self.df.groupby(self.df['entry_time'].dt.strftime('%b %d'))['pnl'].sum().reset_index()
        daily_data = daily.rename(columns={'entry_time': 'date', 'pnl': 'value'}).to_dict('records')

        # 3. Top Instruments
        instruments = self.df.groupby(['symbol', 'direction', 'instrument_type'])['pnl'].sum().reset_index()
        instruments['abs_pnl'] = instruments['pnl'].abs()
        top_instruments = instruments.sort_values('abs_pnl', ascending=False).head(5).to_dict('records')

        # 4. Recent Trades
        recent = self.df.sort_values('entry_time', ascending=False).head(5).copy()
        # Clean datetime for JSON
        recent['entry_time'] = recent['entry_time'].dt.strftime('%Y-%m-%dT%H:%M:%S')
        if 'exit_time' in recent.columns:
             recent['exit_time'] = pd.to_datetime(recent['exit_time'], errors='coerce').dt.strftime('%Y-%m-%dT%H:%M:%S').fillna('')
        recent_trades = recent.to_dict('records')

        return {
            "netPL": round(total_pnl, 2),
            "winRate": round(win_rate, 1),
            "profitFactor": round(profit_factor, 2),
            "expectancy": round(expectancy, 2),
            "maxDrawdown": round(max_drawdown, 2),
            "avgWin": round(avg_win, 2),
            "avgLoss": round(avg_loss, 2),
            "tradeCount": total_count,
            "cumulativeData": cumulative_data,
            "dailyData": daily_data,
            "topInstruments": top_instruments,
            "recentTrades": recent_trades,
            # Placeholder for strategies (requires JOIN in DB or mapping)
            "strategyPerformance": [] 
        }

    def _empty_stats(self):
        return {
            "netPL": 0, "winRate": 0, "profitFactor": 0, "expectancy": 0, "maxDrawdown": 0,
            "avgWin": 0, "avgLoss": 0, "tradeCount": 0,
            "cumulativeData": [], "dailyData": [], "topInstruments": [], "recentTrades": [], "strategyPerformance": []
        }