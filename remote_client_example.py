import zmq

context = zmq.Context()

#  Socket to talk to server
print("Connecting to hello world server…")
socket = context.socket(zmq.REQ)
socket.connect("tcp://localhost:9779")

out_command = ""
while out_command.lower() != "exit":
    out_command = input("Command to serial port: ")
    socket.send_string(out_command)

    #  Get the reply.
    message = socket.recv()
    print("Received response for: %s [ %s ]" % (out_command.encode('ascii'), message.decode('ascii')))
