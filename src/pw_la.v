/*
 * Logic analyzer: samples the 8 GPIO pins into a ring buffer inside the
 * instruction memory (two samples per 16-bit word, first sample in the high
 * byte). Keeps running until a trigger, then captures `post` more words and
 * stops, so the buffer holds both what came before and after the trigger.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_la (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        arm,         // pulse: start capturing
    input  wire        stop,        // pulse: stop now
    // config
    input  wire [6:0]  start,       // first word of the buffer
    input  wire [6:0]  last,        // last word of the buffer
    input  wire [5:0]  post,        // words to capture after the trigger
    input  wire [15:0] div,         // sample every div + 1 clocks
    input  wire [7:0]  trig_mask,
    input  wire [7:0]  trig_value,
    input  wire [1:0]  trig_mode,   // 0 now, 1 pattern match, 2 pattern change, 3 flag 3
    // inputs
    input  wire [7:0]  pins,
    input  wire        flag3,
    // memory write port
    output reg         we,
    output reg  [6:0]  waddr,
    output reg  [15:0] wdata,
    // status
    output reg  [1:0]  state,       // 0 idle, 1 waiting for trigger, 2 triggered, 3 done
    output reg  [6:0]  ptr,         // next word to write
    output reg  [6:0]  trig_word,   // word that holds the trigger sample
    output reg         trig_half    // 0 = trigger sample is the high byte, 1 = low byte
);
  localparam IDLE = 2'd0, WAIT = 2'd1, POST = 2'd2, DONE = 2'd3;

  reg [15:0] cnt;
  reg [7:0]  first;   // first sample of the current word
  reg        half;    // 1 = the next sample completes a word
  reg [7:0]  prev;
  reg [5:0]  left;

  wire running = (state == WAIT) || (state == POST);
  wire sample  = running && (cnt == 16'd0);

  reg hit;
  always @(*) begin
    case (trig_mode)
      2'd0:    hit = 1'b1;
      2'd1:    hit = ((pins ^ trig_value) & trig_mask) == 8'd0;
      2'd2:    hit = ((pins ^ prev) & trig_mask) != 8'd0;
      default: hit = flag3;
    endcase
  end

  wire [6:0] next_ptr = (ptr == last) ? start : ptr + 7'd1;

  always @(posedge clk) begin
    we <= 1'b0;
    if (!rst_n) begin
      state <= IDLE; ptr <= 7'd0; trig_word <= 7'd0; trig_half <= 1'b0;
      cnt <= 16'd0; first <= 8'd0; half <= 1'b0; prev <= 8'd0; left <= 6'd0;
      waddr <= 7'd0; wdata <= 16'd0;
    end else if (arm) begin
      state <= WAIT; ptr <= start; cnt <= 16'd0; half <= 1'b0; prev <= pins;
    end else if (stop) begin
      state <= IDLE;
    end else if (sample) begin
      cnt  <= div;
      prev <= pins;
      if (state == WAIT && hit) begin
        state <= POST; trig_word <= ptr; trig_half <= half; left <= post;
      end
      if (!half) begin
        first <= pins;
        half  <= 1'b1;
      end else begin
        half  <= 1'b0;
        we    <= 1'b1;
        waddr <= ptr;
        wdata <= {first, pins};
        ptr   <= next_ptr;
        // the word that holds the trigger counts as the first post word
        if (state == POST || (state == WAIT && hit)) begin
          if ((state == POST ? left : post) == 6'd0) state <= DONE;
          else left <= (state == POST ? left : post) - 6'd1;
        end
      end
    end else if (running) begin
      cnt <= cnt - 16'd1;
    end
  end
endmodule
