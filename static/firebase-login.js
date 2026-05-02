"use strict";

import { initializeApp } from "https://www.gstatic.com/firebasejs/12.9.0/firebase-app.js";
import {
  createUserWithEmailAndPassword,
  getAuth,
  signInWithEmailAndPassword,
  signOut,
} from "https://www.gstatic.com/firebasejs/12.9.0/firebase-auth.js";


const firebaseConfig = {
  apiKey: "AIzaSyAKwjXFywzsWzsf3JiPv5hf2tEu5cVxpCM",
  authDomain: "twitter-32c8d.firebaseapp.com",
  projectId: "twitter-32c8d",
  storageBucket: "twitter-32c8d.firebasestorage.app",
  messagingSenderId: "876954906499",
  appId: "1:876954906499:web:315fbe83f4877d714610b6"
};



window.addEventListener("load", function () {
  initializeApp(firebaseConfig);
  const auth = getAuth();
  updateUI(document.cookie);

  // signup of a new user to firebase
  document.getElementById("sign-up").addEventListener("click", function () {
    const email = document.getElementById("email").value;
    const password = document.getElementById("password").value;

    createUserWithEmailAndPassword(auth, email, password)
      .then((userCredential) => {
        // we have created a user
        const user = userCredential.user;

        // get the id token for the user who just logged in and force a redirect to /
        user.getIdToken().then((token) => {
          document.cookie = "token=" + token + ";path=/;SameSite=Strict";
          window.location = "/";
        });
      })
      .catch((error) => {
        // issue for signup that we will drop to console
        console.log(error.code + error.message);
      });
  });

  // login of a user to firebase
  document.getElementById("login").addEventListener("click", function () {
    const email = document.getElementById("email").value;
    const password = document.getElementById("password").value;

    signInWithEmailAndPassword(auth, email, password)
      .then((userCredential) => {
        // we have a signed in user
        const user = userCredential.user;
        console.log("logged in");

        // get the id token for the user who just logged in and force a redirect to /
        user.getIdToken().then((token) => {
          document.cookie = "token=" + token + ";path=/;SameSite=Strict";
          window.location = "/";
        });
      })
      .catch((error) => {
        // issue with signin that we will drop to console
        console.log(error.code + error.message);
      });
  });

  // signout from firebase
  document.getElementById("sign-out").addEventListener("click", function () {
    signOut(auth).then(() => {
      // remove the ID token for the user and force a redirect to /
      document.cookie = "token=;path=/;SameSite=Strict";
      window.location = "/";
    });
  });
});

// function that will update the UI for the user depending on if they are logged in or not by checking the passed in cookie
// that contains the token
function updateUI(cookie) {
  const token = parseCookieToken(cookie);

  // if a user is logged in then disable the email, password, signup, and login UI elements and show the signout button and vice versa
  if (token.length > 0) {
    document.getElementById("login-box").hidden = true;
    document.getElementById("sign-out").hidden = false;
  } else {
    document.getElementById("login-box").hidden = false;
    document.getElementById("sign-out").hidden = true;
  }
}

// function that will take the cookie and will return the value associated with it to the caller
function parseCookieToken(cookie) {
  // split the cookie out on the basis of the semi-colon
  const strings = cookie.split(";");

  // go through each of the strings
  for (let i = 0; i < strings.length; i += 1) {
    // split the string based on the = sign. if the LHS is token then return the RHS immediately
    const temp = strings[i].trim().split("=");
    if (temp[0] === "token") {
      return temp[1];
    }
  }

  // if we get to this point then the token wasn't in the cookie so return the empty string
  return "";
}