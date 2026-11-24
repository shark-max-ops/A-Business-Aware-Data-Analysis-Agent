import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { App } from './App';
import './style.css';

const theme = {
  token: {
    colorPrimary: '#9b4e36',
    colorPrimaryHover: '#b56345',
    colorPrimaryActive: '#84402b',
    colorInfo: '#4d806b',
    colorSuccess: '#4d806b',
    colorWarning: '#a68b5a',
    colorError: '#b05a4d',
    colorText: '#2c2a28',
    colorTextSecondary: '#686460',
    colorTextTertiary: '#8a8378',
    colorBgContainer: '#fffefa',
    colorBgElevated: '#fffefa',
    colorBgLayout: '#f5f3f0',
    colorBorder: '#e2dcd4',
    colorBorderSecondary: '#ece7e0',
    colorFillAlter: '#f7f4ee',
    colorFillSecondary: '#f1ede6',
    borderRadius: 9,
    borderRadiusLG: 12,
    fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
    controlHeight: 36,
    boxShadowSecondary: '0 8px 24px rgba(54, 47, 39, 0.08), 0 2px 6px rgba(54, 47, 39, 0.04)',
  },
  components: {
    Button: { defaultBorderColor: '#ded7cd', defaultBg: '#fffefa', defaultColor: '#3a3632', defaultHoverBorderColor: '#bfa999', defaultHoverColor: '#9b4e36' },
    Select: { optionSelectedBg: '#efe9e0', optionActiveBg: '#f6f2eb', selectorBg: '#fffefa' },
    Table: { headerBg: '#f6f3ee', headerColor: '#686460', rowHoverBg: '#faf7f2', borderColor: '#ebe6df', headerSplitColor: '#e6e0d8' },
    Drawer: { colorBgElevated: '#fdfcf9' },
    Modal: { contentBg: '#fffefa', headerBg: '#fffefa' },
    Alert: { borderRadiusLG: 10 },
  },
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={theme}><BrowserRouter><App /></BrowserRouter></ConfigProvider></React.StrictMode>,
);
