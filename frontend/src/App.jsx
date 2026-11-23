import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth, RequireCap } from "./components/Layout";
import { Audit, Settings, Staff } from "./pages/Admin";
import { CustomerDetail, Customers } from "./pages/Customers";
import { Analytics, Dashboard } from "./pages/Dashboard";
import { FollowUpDetail, FollowUps } from "./pages/FollowUps";
import { Login, TwoFactor } from "./pages/Login";
import { Messages } from "./pages/Messages";
import { Payments } from "./pages/Payments";
import { ReportRequestDetail, ReportRequests } from "./pages/ReportRequests";

const guarded = (cap, element) => <RequireCap cap={cap}>{element}</RequireCap>;

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/2fa" element={<TwoFactor />} />
      <Route element={<RequireAuth />}>
        <Route index element={guarded("dashboard.view", <Dashboard />)} />
        <Route path="follow-ups" element={guarded("followup.view", <FollowUps />)} />
        <Route path="follow-ups/:id" element={guarded("followup.view", <FollowUpDetail />)} />
        <Route path="report-requests" element={guarded("report.view", <ReportRequests />)} />
        <Route path="report-requests/:id" element={guarded("report.view", <ReportRequestDetail />)} />
        <Route path="payments" element={guarded("payment.view", <Payments />)} />
        <Route path="customers" element={guarded("customer.view", <Customers />)} />
        <Route path="customers/:id" element={guarded("customer.view", <CustomerDetail />)} />
        <Route path="messages" element={guarded("customer.view", <Messages />)} />
        <Route path="analytics" element={guarded("analytics.revenue", <Analytics />)} />
        <Route path="staff" element={guarded("staff.manage", <Staff />)} />
        <Route path="audit" element={guarded("audit.view", <Audit />)} />
        <Route path="settings" element={guarded("settings.manage", <Settings />)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
