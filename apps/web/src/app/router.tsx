import { createBrowserRouter } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { MarketPage } from "@/pages/MarketPage/MarketPage";
import { StrategyPage } from "@/pages/StrategyPage/StrategyPage";
import { TradePage } from "@/pages/TradePage/TradePage";
import { BacktestPage } from "@/pages/BacktestPage/BacktestPage";
import { AlertsPage } from "@/pages/AlertsPage/AlertsPage";
import { SettingsPage } from "@/pages/SettingsPage/SettingsPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <MarketPage /> },
      { path: "strategy", element: <StrategyPage /> },
      { path: "trade", element: <TradePage /> },
      { path: "backtest", element: <BacktestPage /> },
      { path: "alerts", element: <AlertsPage /> },
      { path: "settings", element: <SettingsPage /> },
    ],
  },
]);
