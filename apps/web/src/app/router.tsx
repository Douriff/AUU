import { createBrowserRouter, Navigate } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { AdminShell } from "@/components/layout/AdminShell";
import { BoardPage } from "@/pages/BoardPage/BoardPage";
import { ConsolePage } from "@/pages/ConsolePage/ConsolePage";
import { MarketsPage } from "@/pages/MarketsPage/MarketsPage";
import { MarketPage } from "@/pages/MarketPage/MarketPage";
import { ReviewPage } from "@/pages/ReviewPage/ReviewPage";
import { StrategyPage } from "@/pages/StrategyPage/StrategyPage";
import { PositionsPage } from "@/pages/PositionsPage/PositionsPage";
import { TradingPage } from "@/pages/TradingPage/TradingPage";
import { LeaderboardPage } from "@/pages/LeaderboardPage/LeaderboardPage";
import { LoginPage } from "@/pages/LoginPage/LoginPage";
import { RegisterPage } from "@/pages/LoginPage/RegisterPage";
import { ForgotPasswordPage } from "@/pages/LoginPage/ForgotPasswordPage";
import { MajorsPage } from "@/pages/MajorsPage/MajorsPage";
import { BacktestPage } from "@/pages/BacktestPage/BacktestPage";
import { AlertsPage } from "@/pages/AlertsPage/AlertsPage";
import { WatchPage } from "@/pages/WatchPage/WatchPage";
import { ObservePage } from "@/pages/ObservePage/ObservePage";
import { SettingsPage } from "@/pages/SettingsPage/SettingsPage";
import { MainstreamPage } from "@/pages/MainstreamPage/MainstreamPage";
import { PerformancePage } from "@/pages/PerformancePage/PerformancePage";
import { PnlPage } from "@/pages/PnlPage/PnlPage";
import { StatusPage } from "@/pages/StatusPage/StatusPage";
import { NewsPage } from "@/pages/NewsPage/NewsPage";
import { AirdropsPage } from "@/pages/AirdropsPage/AirdropsPage";
import { AirdropDetailPage } from "@/pages/AirdropsPage/AirdropDetailPage";
import { PointsPage } from "@/pages/PointsPage/PointsPage";
import { AdminPointsPage } from "@/pages/PointsPage/AdminPointsPage";
import { Trc20AdminPanel } from "@/pages/PointsPage/Trc20AdminPanel";
import { AdminWithdrawPanel } from "@/pages/PointsPage/AdminWithdrawPanel";
import { AdminTradingPanel } from "@/pages/PointsPage/AdminTradingPanel";
import { AdminHubPage } from "@/pages/AdminPage/AdminHubPage";
import { AdminLedgerPage } from "@/pages/AdminPage/AdminLedgerPage";
import { AdminFlagsPanel } from "@/pages/AdminPage/AdminFlagsPanel";
import { AdminExchangePanel } from "@/pages/AdminPage/AdminExchangePanel";
import { ExchangePage } from "@/pages/ExchangePage/ExchangePage";
import { AdminSectionPage } from "@/pages/AdminPage/AdminSectionPage";
import { ChartPocPage } from "@/pages/ChartPocPage/ChartPocPage";
import { HomeIndex, LegacyOnly } from "@/app/legacy";

export const router = createBrowserRouter([
  {
    path: "/admin",
    element: <AdminShell />,
    children: [
      { index: true, element: <AdminHubPage /> },
      { path: "users", element: <AdminPointsPage /> },
      { path: "ledger", element: <AdminLedgerPage /> },
      {
        path: "topups",
        element: (
          <AdminSectionPage>
            <Trc20AdminPanel />
          </AdminSectionPage>
        ),
      },
      {
        path: "trading",
        element: (
          <AdminSectionPage>
            <AdminTradingPanel />
          </AdminSectionPage>
        ),
      },
      {
        path: "withdrawals",
        element: (
          <AdminSectionPage>
            <AdminWithdrawPanel />
          </AdminSectionPage>
        ),
      },
      { path: "flags", element: <AdminFlagsPanel /> },
      { path: "exchange", element: <AdminExchangePanel /> },
      // Back-compat: old /admin/points lived inside the trading shell
      { path: "points", element: <Navigate to="/admin/users" replace /> },
    ],
  },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <HomeIndex board={<BoardPage />} mainstream={<MainstreamPage />} /> },
      { path: "mainstream", element: <MainstreamPage /> },
      { path: "board", element: <LegacyOnly><BoardPage /></LegacyOnly> },
      { path: "console", element: <ConsolePage /> },
      { path: "performance", element: <PerformancePage /> },
      { path: "pnl", element: <PnlPage /> },
      { path: "markets", element: <LegacyOnly><MarketsPage /></LegacyOnly> },
      { path: "market", element: <LegacyOnly><MarketPage /></LegacyOnly> },
      { path: "review", element: <LegacyOnly><ReviewPage /></LegacyOnly> },
      { path: "strategy", element: <LegacyOnly><StrategyPage /></LegacyOnly> },
      { path: "trade", element: <LegacyOnly><TradingPage /></LegacyOnly> },
      { path: "trade/:mint", element: <LegacyOnly><TradingPage /></LegacyOnly> },
      { path: "majors", element: <MajorsPage /> },
      { path: "news", element: <NewsPage /> },
      { path: "airdrops", element: <AirdropsPage /> },
      { path: "exchange", element: <ExchangePage /> },
      { path: "airdrops/:id", element: <AirdropDetailPage /> },
      { path: "positions", element: <LegacyOnly><PositionsPage /></LegacyOnly> },
      { path: "leaderboard", element: <LeaderboardPage /> },
      { path: "chart-poc", element: <ChartPocPage /> },
      { path: "login", element: <LoginPage /> },
      { path: "register", element: <RegisterPage /> },
      { path: "forgot-password", element: <ForgotPasswordPage /> },
      { path: "backtest", element: <LegacyOnly><BacktestPage /></LegacyOnly> },
      { path: "watch", element: <LegacyOnly><WatchPage /></LegacyOnly> },
      { path: "watch/:watchId", element: <LegacyOnly><WatchPage /></LegacyOnly> },
      { path: "observe", element: <LegacyOnly><ObservePage /></LegacyOnly> },
      { path: "alerts", element: <LegacyOnly><AlertsPage /></LegacyOnly> },
      { path: "settings", element: <SettingsPage /> },
      { path: "status", element: <StatusPage /> },
      { path: "points", element: <PointsPage /> },
    ],
  },
]);
