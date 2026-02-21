from fastapi import FastAPI
from k16v5 import K16V5

board = K16V5(1,0x20)

app = FastAPI()
@app.get("/")
def home():
    return {"Hello": "FastAPI"}

@app.get("/locker/open/{locker_id}")
def contact_details(locker_id: str):
    if locker_id == "B31":
        board.send_pulse("B", 0)
    if locker_id == "B32":
        board.send_pulse("B", 1)
    if locker_id == "B33":
        board.send_pulse("B", 2)
    if locker_id == "B34":
        board.send_pulse("B", 3)

    return {'response': 'success', 'locker_id': locker_id}

@app.get("/locker/status/{locker_id}")
def contact_details(locker_id: str):
    return {'open': 'true'}
