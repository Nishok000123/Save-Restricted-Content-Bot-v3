import os
from flask import Flask, render_template

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), '..', 'templates'))

@app.route("/")
def welcome():
    return render_template("welcome.html")

@app.route("/health")
def health():
    return {"status": "ok"}, 200
