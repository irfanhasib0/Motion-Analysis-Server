/* global firebase */
importScripts("https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js");

// Same config as index.html
firebase.initializeApp({
  apiKey: "AIzaSyD4RIGlIKB0MPqcRjJjPGBH3t18SaHMYXE",
  authDomain: "nvr-101.firebaseapp.com",
  projectId: "nvr-101",
  storageBucket: "nvr-101.firebasestorage.app",
  messagingSenderId: "185157179899",
  appId: "1:185157179899:web:13936bc69aa75664b1935e",
  measurementId: "G-DG9VES3DEL"
});

const messaging = firebase.messaging();

// Background notifications
messaging.onBackgroundMessage((payload) => {
  const title = payload?.notification?.title || "FCM Background";
  const options = {
    body: payload?.notification?.body || JSON.stringify(payload),
  };
  self.registration.showNotification(title, options);
});
