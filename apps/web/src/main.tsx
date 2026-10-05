import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { router } from "@/app/router";
import { WalletProviders } from "@/wallet/WalletProviders";
import "@/wallet/solanaBuffer";
import "@/styles.css";
import "@/styles/pro.css";
import { applyColorPref } from "@/theme/colorPref";
import { initI18n } from "@/i18n";

applyColorPref();

// Load the interface language before the first paint so no Chinese flashes for other languages.
void initI18n().finally(() => ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <WalletProviders>
      <RouterProvider router={router} />
    </WalletProviders>
  </React.StrictMode>
));
