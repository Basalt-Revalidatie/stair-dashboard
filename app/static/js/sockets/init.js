// startup socket connection
socket = io.connect(location.protocol + '//' + document.domain + ':' + location.port);
// console.log(socket);

socket.on('connect', function () {
    console.log('connected!');
})

// Message when there is a connection error
socket.on("connect_error", (err) => {
    console.log(`connect_error due to ${err.message}`);
});
