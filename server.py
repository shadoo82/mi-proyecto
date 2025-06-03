from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/orderbook')
def orderbook():
    return render_template('orderbook.html')

if __name__ == '__main__':
    app.run(debug=True)
     
