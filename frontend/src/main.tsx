import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { App } from './App';
import './style.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#b56345', colorText: '#34322d', colorBgContainer: '#fffefa', colorBorder: '#dfdcd4', borderRadius: 10, fontFamily: 'Inter, "Microsoft YaHei", sans-serif', controlHeight: 38 } }}><BrowserRouter><App /></BrowserRouter></ConfigProvider></React.StrictMode>,
);
