import { createBrowserRouter } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
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
import { StatusPage } from "@/pages/StatusPage/StatusPage";
import { HomeIndex, LegacyOnly } from "@/app/legacy";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <HomeIndex board={<BoardPage />} mainstream={<MainstreamPage />} /> },
      { path: "mainstream", element: <MainstreamPage /> },
      { path: "board", element: <LegacyOnly><BoardPage /></LegacyOnly> },
      { path: "console", element: <ConsolePage /> },
      { path: "performance", element: <PerformancePage /> },
      { path: "markets", element: <LegacyOnly><MarketsPage /></LegacyOnly> },
      { path: "market", element: <LegacyOnly><MarketPage /></LegacyOnly> },
      { path: "review", element: <LegacyOnly><ReviewPage /></LegacyOnly> },
      { path: "strategy", element: <LegacyOnly><StrategyPage /></LegacyOnly> },
      { path: "trade", element: <LegacyOnly><TradingPage /></LegacyOnly> },
      { path: "trade/:mint", element: <LegacyOnly><TradingPage /></LegacyOnly> },
      { path: "majors", element: <MajorsPage /> },
      { path: "positions", element: <LegacyOnly><PositionsPage /></LegacyOnly> },
      { path: "leaderboard", element: <LeaderboardPage /> },
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
    ],
  },
]);
