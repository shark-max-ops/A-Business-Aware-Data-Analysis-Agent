import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { App } from './App';
import './style.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#226b58', borderRadius: 10, fontFamily: 'Inter, "Microsoft YaHei", sans-serif' } }}><BrowserRouter><App /></BrowserRouter></ConfigProvider></React.StrictMode>,
);
