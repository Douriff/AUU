import { createBrowserRouter } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { BoardPage } from "@/pages/BoardPage/BoardPage";
import { MarketsPage } from "@/pages/MarketsPage/MarketsPage";
import { MarketPage } from "@/pages/MarketPage/MarketPage";
import { ReviewPage } from "@/pages/ReviewPage/ReviewPage";
import { StrategyPage } from "@/pages/StrategyPage/StrategyPage";
import { TradePage } from "@/pages/TradePage/TradePage";
import { BacktestPage } from "@/pages/BacktestPage/BacktestPage";
import { AlertsPage } from "@/pages/AlertsPage/AlertsPage";
import { WatchPage } from "@/pages/WatchPage/WatchPage";
import { ObservePage } from "@/pages/ObservePage/ObservePage";
import { SettingsPage } from "@/pages/SettingsPage/SettingsPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <BoardPage /> },
      { path: "markets", element: <MarketsPage /> },
      { path: "market", element: <MarketPage /> },
      { path: "review", element: <ReviewPage /> },
      { path: "strategy", element: <StrategyPage /> },
      { path: "trade", element: <TradePage /> },
      { path: "backtest", element: <BacktestPage /> },
      { path: "watch", element: <WatchPage /> },
      { path: "watch/:watchId", element: <WatchPage /> },
      { path: "observe", element: <ObservePage /> },
      { path: "alerts", element: <AlertsPage /> },
      { path: "settings", element: <SettingsPage /> },
    ],
  },
]);
